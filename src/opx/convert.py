"""Convert a ROM into an OPxFlasher full ROM.

Input: anything opx.rom.ota.open_rom reads that holds a complete image set: a
full OTA zip / payload.bin, an image folder, a fastboot flasher-kit zip, or a
super.img set. Output:

- ``<name>-fullrom-<REGION>.zip``: every image, raw (sparse expanded), split into
  parts of at most 1 GiB (recovery ``unzip`` tools struggle with bigger
  entries), plus an installer (``META-INF/com/google/android/update-binary``,
  ``opx/recovery/rom-install.sh``) for TWRP-based recoveries (OrangeFox,
  TWRP, AERA). The installer refuses unless the phone is the right board and
  variant, isn't mid-update, and the ROM is not older than the installed
  Android (anti-rollback fuse); it verifies every part, writes, and reads back.
- ``<name>-fullrom-<REGION>/``: the same images as plain files (fastboot / OPxFlasher).

Both carry ``opxflasher-rom.json`` (identity, sizes, SHA-256 of every image and
part), so OPxFlasher reads them with the same checks as the original OTA.
Identity and personal-data partitions (profile ``never_flash``) are never put in.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from opx import __version__, lp
from opx.flash import AVB_ORDER_LAST
from opx.rom.ota import (
    FULLROM_JSON,
    RomError,
    RomImage,
    RomInfo,
    is_generic_fingerprint,
    materialize,
    sha256_file,
)

CHUNK = 1 << 30
FORMAT = "opxflasher-fullrom"
SCRIPT = Path(__file__).parent / "recovery" / "rom-install.sh"
_SAFE = re.compile(r"^[A-Za-z0-9._()+-]+$")
META_KEYS = ("ota-type", "pre-device", "post-build", "post-build-incremental", "post-security-patch-level",
             "post-timestamp")

Progress = Callable[[str, int, int], None]  # (what, done, total)


class ConvertError(RuntimeError):
    pass


@dataclass
class Item:
    name: str
    kind: str  # "physical", "logical" or "auto" (decided on the phone: by-name partition, else logical)
    size: int
    image: RomImage


@dataclass
class Plan:
    items: list[Item]
    skipped: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    recovery_ok: bool = True
    recovery_problem: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)


def _profile(rom: RomInfo):
    from opx import profile as profiles

    return profiles.detect({"ro.product.model": rom.product}) if rom.product else profiles.load("oplus-generic")


def _unfold_super(rom: RomInfo, workdir: Path) -> None:
    """A super.img the reader couldn't look into yet (sparse, split, in a zip): expand it
    once and list its logical partitions, so each is converted on its own."""
    sup = rom.image("super")
    if sup is None or any(i.in_super is not None for i in rom.images):
        return
    raw = workdir / "super.raw.img"
    if not (sup.file is not None and not sup.sparse and not sup.pieces):
        materialize(rom, "super", raw)
        src = raw
    else:
        src = sup.file
    try:
        md = lp.read_super_file(src)
    except (lp.LpError, OSError) as e:
        raise ConvertError(f"super.img's partition table can't be read ({e}).") from e
    slots = {lp.strip_slot(p.name)[1] for p in md.partitions if p.size}
    slot = "_a" if "_a" in slots else next(iter(slots), "")
    for p in md.partitions:
        base, sfx = lp.strip_slot(p.name)
        if p.size and sfx == slot and rom.image(base) is None:
            rom.images.append(RomImage(name=base, size=p.size, logical=True, in_super=p))
    rom.super_raw = src  # where materialize() finds the partitions' data


def plan(rom: RomInfo) -> Plan:
    if rom.is_full is False:
        raise ConvertError("This is an incremental OTA: it only holds the changes from one build to the "
                           "next, so it can't become a full ROM. Use the full OTA of the build you want.")
    prof = _profile(rom)
    has_parts = any(i.in_super is not None for i in rom.images)
    items, skipped = [], []
    for img in rom.images:
        if img.name == "super" and has_parts:
            continue
        if prof.is_never_flash(img.name):
            skipped.append(img.name)
            continue
        if img.name == "super":
            raise ConvertError("super.img must be opened first (convert() does this).")
        if rom.payload is not None or (rom.kind == "fullrom" and not img.guessed) or img.in_super is not None:
            kind = "logical" if img.logical else "physical"
        else:
            kind = "auto"
        items.append(Item(img.name, kind, img.size, img))
    if not items:
        raise ConvertError("There are no images to convert.")
    items.sort(key=lambda i: (i.kind == "physical" and i.name in AVB_ORDER_LAST,
                              AVB_ORDER_LAST.index(i.name) if i.name in AVB_ORDER_LAST else 0))
    p = Plan(items, skipped)
    p.boards = list(dict.fromkeys(b for b in [rom.codename, *prof.recovery_boards] if b))
    meta = rom.metadata
    if not (rom.codename and rom.product) or is_generic_fingerprint(rom.fingerprint):
        p.recovery_ok, p.recovery_problem = False, (
            "the ROM doesn't say which phone it's for, so a recovery couldn't check it")
    elif not (meta.get("post-timestamp", "").isdigit() or rom.security_patch):
        p.recovery_ok, p.recovery_problem = False, (
            "the ROM doesn't say how new it is, so a recovery couldn't rule out a downgrade")
    else:
        bad = [v for v in [*p.boards, rom.product, *(i.name for i in items)] if not _SAFE.match(v)]
        if bad:
            p.recovery_ok, p.recovery_problem = False, f"'{bad[0]}' can't be checked safely by a recovery script"
    if any(i.kind == "auto" for i in items):
        p.warnings.append("Which images are logical (inside super) is guessed from their names; the installer "
                          "checks each one against the phone before writing.")
    return p


REGION_CODES = {"china": "CN", "india": "IN", "eu": "EU", "europe": "EU", "na": "NA", "north america": "NA",
                "global": "GLO", "indonesia": "ID", "japan": "JP", "taiwan": "TW", "malaysia": "MY"}


@functools.lru_cache(maxsize=1)
def _all_variants() -> dict[str, str]:
    """Model number -> region name, from every phone profile."""
    from opx import profile as profiles

    out: dict[str, str] = {}
    for pid in profiles.all_profile_ids():
        try:
            out.update(profiles.load(pid).variants or {})
        except (OSError, ValueError):
            continue
    return out


def region_code(rom: RomInfo) -> str:
    """Short region of the ROM (CN, IN, EU, NA, GLO ...) for file names; "" when it can't be told.
    From the phone profile's variants (model -> region), else the build's region tag (CN01)."""
    product = (rom.product or "").upper()
    if product:
        variants = _all_variants()
        for model in sorted(variants, key=len, reverse=True):  # CPH2649IN -> CPH2649
            if product.startswith(model.upper()):
                name = str(variants[model]).strip()
                return REGION_CODES.get(name.lower(), re.sub(r"[^A-Z]", "", name.upper())[:3])
    meta = rom.metadata or {}
    for text in (rom.path.name, meta.get("post-display-id", ""), meta.get("post-build-incremental", "")):
        m = re.search(r"\((CN)\d\d\)", text or "")  # (EX01) etc. are shared by several regions
        if m:
            return m.group(1)
    return ""


_SUFFIX = re.compile(r"-fullrom(?:-[A-Z]{2,3})?$")


def output_name(rom: RomInfo) -> str:
    stem = rom.path.name
    for ext in (".zip", ".bin"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
    if stem in ("payload", "OTA_FILES_HERE", "images") and rom.product:
        stem = f"{rom.product}_{rom.build_incremental or rom.android}"
    stem = _SUFFIX.sub("", stem)
    return re.sub(r"[^A-Za-z0-9._()+-]+", "_", stem).strip("_") or "rom"


def output_paths(rom: RomInfo, out_dir: Path) -> tuple[Path, Path]:
    """(recovery zip, image folder): <name>-fullrom[-REGION].zip and <name>-fullrom[-REGION]."""
    region = region_code(rom)
    tail = f"-fullrom-{region}" if region else "-fullrom"
    base = output_name(rom) + tail
    return Path(out_dir) / f"{base}.zip", Path(out_dir) / base


def existing_outputs(rom: RomInfo, out_dir: Path, make_zip: bool = True, make_folder: bool = False) -> dict[str, Path]:
    """Outputs convert() would make that are already there: {"zip": path, "folder": path}."""
    zip_out, folder_out = output_paths(rom, out_dir)
    want = {"zip": (make_zip, zip_out), "folder": (make_folder, folder_out)}
    return {kind: path for kind, (wanted, path) in want.items() if wanted and path.exists()}


def verify_existing(paths: dict[str, Path], progress: Progress | None = None,
                    cancel: Callable[[], bool] | None = None) -> dict[str, Path]:
    """Fully verify outputs made earlier before they're used again. Raises ConvertError if one is bad."""
    from opx.rom import verify as vf

    for path in paths.values():
        try:
            rep = vf.verify(path, deep=True, cancel=cancel,
                            progress=progress)
        except vf.VerifyCancelled:
            raise ConvertError("Cancelled; the existing files were left as they are.") from None
        if rep.failed or rep.detection.rom is None or rep.detection.rom.kind != "fullrom":
            why = next((f.text for f in rep.sorted_findings() if f.status == vf.FAIL), "not an OPxFlasher full ROM")
            raise ConvertError(f"The existing {path.name} failed verification ({why}). Replace it.")
    return dict(paths)


def convert_or_reuse(rom: RomInfo, out_dir: Path, make_zip: bool = True, make_folder: bool = False,
                     progress: Progress | None = None, stage: Callable[[str], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> dict[str, Path]:
    """Like convert(), but outputs that already exist are verified and reused, not refused."""
    have = existing_outputs(rom, out_dir, make_zip, make_folder)
    if have:
        (stage or (lambda s: None))("Already converted: verifying " + ", ".join(p.name for p in have.values()) + "...")
        verify_existing(have, progress, cancel)
    need_zip, need_folder = make_zip and "zip" not in have, make_folder and "folder" not in have
    made = convert(rom, out_dir, need_zip, need_folder, progress, stage, cancel) if need_zip or need_folder else {}
    return {**have, **made}


def convert(rom: RomInfo, out_dir: Path, make_zip: bool = True, make_folder: bool = False,
            progress: Progress | None = None, stage: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None, chunk: int = CHUNK, replace: bool = False) -> dict[str, Path]:
    """Write the full ROM. Returns {"zip": path, "folder": path} for what was made.
    Existing outputs are refused unless `replace` (then they're deleted first)."""
    if not (make_zip or make_folder):
        raise ConvertError("Choose a recovery zip, an image folder, or both.")
    if chunk <= 0 or chunk % (1 << 20):
        raise ConvertError("Parts must be whole MiB.")
    stage = stage or (lambda s: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_out, folder_out = output_paths(rom, out_dir)
    for target in ([zip_out] if make_zip else []) + ([folder_out] if make_folder else []):
        if target.exists() and replace:
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        elif target.exists():
            raise ConvertError(f"{target.name} already exists in {out_dir}; move it or choose another folder.")

    with tempfile.TemporaryDirectory(prefix="opx-convert-", dir=out_dir) as tmp:
        work = Path(tmp)
        stage("Reading the ROM...")
        try:
            _unfold_super(rom, work)
            p = plan(rom)
        except (RomError, OSError) as e:
            raise ConvertError(str(e)) from e
        if make_zip and not p.recovery_ok:
            raise ConvertError(f"Can't make a recovery zip: {p.recovery_problem}. An image folder can still be made "
                               "(OPxFlasher checks it against the phone before flashing).")
        largest = max(i.size for i in p.items)
        need = p.total * (int(make_zip) + int(make_folder)) + (0 if make_folder else largest) + (64 << 20)
        free = shutil.disk_usage(out_dir).free
        if free < need:
            raise ConvertError(f"Not enough free space in {out_dir}: needs about {need / 1e9:.1f} GB, "
                               f"{free / 1e9:.1f} GB free.")
        for w in p.warnings:
            stage(w)
        if p.skipped:
            stage(f"Left out (never written by OPxFlasher): {', '.join(p.skipped)}")

        total = p.total * (2 if make_zip else 1)
        done = 0

        def tick(what: str, n: int) -> None:
            nonlocal done
            done += n
            if progress:
                progress(what, done, total)
            if cancel and cancel():
                raise ConvertError("Cancelled; nothing was kept.")

        made: dict[str, Path] = {}
        building_folder = out_dir / f".{folder_out.name}.part"
        building_zip = zip_out.with_name(zip_out.name + ".part")
        try:
            src_dir = building_folder if make_folder else work
            src_dir.mkdir(exist_ok=True)
            records = []
            zf = zipfile.ZipFile(building_zip, "w", allowZip64=True) if make_zip else None
            try:
                if zf is not None:
                    script = zipfile.ZipInfo("META-INF/com/google/android/update-binary")
                    script.external_attr = 0o755 << 16
                    zf.writestr(script, SCRIPT.read_bytes().replace(b"\r\n", b"\n"), zipfile.ZIP_DEFLATED)
                    zf.writestr("META-INF/com/google/android/updater-script", "# OPxFlasher: see update-binary\n")
                for n, item in enumerate(p.items, 1):
                    stage(f"{item.name} ({n} of {len(p.items)})")
                    img = item.image
                    direct = img.file is not None and not img.sparse and not img.pieces and not make_folder
                    if direct:
                        path = img.file
                        sha = img.sha256 or ""
                        if not sha:
                            h = hashlib.sha256()
                            with open(path, "rb") as fh:
                                while data := fh.read(1 << 20):
                                    h.update(data)
                                    tick(item.name, len(data))
                            sha = h.hexdigest()
                        else:
                            tick(item.name, item.size)
                    else:
                        path = src_dir / f"{item.name}.img"
                        materialize(rom, item.name, path, lambda k, w=item.name: tick(w, k))
                        sha = sha256_file(path)
                        if img.sha256 and sha != img.sha256:
                            raise ConvertError(f"{item.name} doesn't match its recorded SHA-256.")
                    rec = {"name": item.name, "kind": item.kind, "size": item.size, "sha256": sha,
                           "file": f"{item.name}.img", "parts": []}
                    if zf is not None:
                        rec["parts"] = _add_parts(zf, path, item, chunk, tick)
                        if not direct and not make_folder:
                            path.unlink()
                    records.append(rec)

                manifest = {
                    "format": FORMAT, "version": 1, "tool": f"OPxFlasher {__version__}",
                    "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": {"name": rom.path.name, "kind": rom.kind},
                    "metadata": {k: rom.metadata[k] for k in META_KEYS if rom.metadata.get(k)},
                    "product": rom.product, "device": rom.codename, "boards": p.boards,
                    "chunk": chunk, "images": records, "left_out": p.skipped,
                }
                if zf is not None:
                    zf.writestr("opx/rom.list", _install_list(rom, p, records, chunk))
                    zf.writestr(FULLROM_JSON, json.dumps(manifest, indent=2))
                    zf.writestr("SHA256SUMS", "".join(f"{pt['sha256']}  {pt['entry']}\n"
                                                       for r in records for pt in r["parts"]))
            finally:
                if zf is not None:
                    zf.close()
            if make_folder:
                folder_manifest = dict(manifest, images=[{k: v for k, v in r.items() if k != "parts"}
                                                         for r in records])
                (building_folder / FULLROM_JSON).write_text(json.dumps(folder_manifest, indent=2), encoding="utf-8")
                (building_folder / "SHA256SUMS").write_text(
                    "".join(f"{r['sha256']}  {r['file']}\n" for r in records), encoding="utf-8")
                building_folder.rename(folder_out)
                made["folder"] = folder_out
            if make_zip:
                building_zip.rename(zip_out)
                made["zip"] = zip_out
        except BaseException:
            building_zip.unlink(missing_ok=True)
            shutil.rmtree(building_folder, ignore_errors=True)
            for path in made.values():
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
            raise
    stage("Done: " + ", ".join(p.name for p in made.values()))
    return made


def _add_parts(zf: zipfile.ZipFile, path: Path, item: Item, chunk: int, tick) -> list[dict]:
    parts = []
    count = max(1, -(-item.size // chunk))
    with open(path, "rb") as fh:
        for index in range(count):
            entry = f"images/{item.name}.img" + (f".{index:03d}" if count > 1 else "")
            length = min(chunk, item.size - index * chunk) if item.size else 0
            info = zipfile.ZipInfo(entry, date_time=datetime.now(timezone.utc).timetuple()[:6])
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            h = hashlib.sha256()
            with zf.open(info, "w", force_zip64=length >= 0xFFFFFFFF) as dst:
                left = length
                while left:
                    data = fh.read(min(1 << 20, left))
                    if not data:
                        raise ConvertError(f"{item.name} is shorter than expected.")
                    dst.write(data)
                    h.update(data)
                    left -= len(data)
                    tick(item.name, len(data))
            parts.append({"entry": entry, "sha256": h.hexdigest(), "size": length})
    return parts


def _install_list(rom: RomInfo, p: Plan, records: list[dict], chunk: int) -> str:
    meta = rom.metadata
    lines = [f"product {rom.product}", f"device {rom.codename}", f"chunk {chunk}"]
    lines += [f"board {b}" for b in p.boards]
    if meta.get("post-timestamp", "").isdigit():
        lines.append(f"timestamp {meta['post-timestamp']}")
    if rom.security_patch:
        lines.append(f"patch {rom.security_patch}")
    fp = rom.fingerprint
    if fp and " " not in fp:
        lines.append(f"fingerprint {fp}")
    shown = re.sub(r"[^A-Za-z0-9._()-]", "_", rom.build_incremental or rom.android or "unknown")
    lines.append(f"build {shown}")
    for r in records:
        lines.append(f"image {r['kind']} {r['name']} {r['size']} {r['sha256']}")
        for i, pt in enumerate(r["parts"]):
            lines.append(f"part {r['name']} {i} {pt['entry']} {pt['sha256']} {pt['size']}")
    return "\n".join(lines) + "\n"
