"""Convert a ROM into an OPxFlasher full ROM.

Input: anything opx.rom.ota.open_rom reads that holds a complete image set: a
full OTA zip / payload.bin, an image folder, a fastboot flasher-kit zip, or a
super.img set. Output:

- ``<name>-fullrom-<REGION>.zip`` (``<name>`` ends with the OS version when it's known,
  e.g. ``-17-0-1-202``): every image, raw (sparse expanded), split into
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
from opx.catalog import has_version, version_tag
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
    keep_on_phone: bool = False  # left out of the recovery zip: the phone keeps its own copy
    backup_file: Path | None = None  # convert ready: written from the user's backup instead of the ROM
    backup_sha: str = ""


@dataclass
class Plan:
    items: list[Item]
    skipped: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    recovery_ok: bool = True
    recovery_problem: str = ""
    warnings: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # network files the recovery zip leaves to the phone
    net_from_rom: list[str] = field(default_factory=list)  # network files written from the ROM (vbmeta checks them)
    ready: dict = field(default_factory=dict)  # convert ready: {"serial", "model", "files": [names from the backup]}
    recovery_kept: bool = False  # the zip leaves the phone's (custom) recovery alone
    cn_kept: list[str] = field(default_factory=list)  # China hardware: the phone's own abl / engineering_cdt stay

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def zip_total(self) -> int:
        return sum(i.size for i in self.items if not i.keep_on_phone)


def _profile(rom: RomInfo):
    from opx import profile as profiles

    return profiles.for_product(rom.product)


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
    # Network / region files (oplusstanvbk ...): the recovery zip keeps the phone's own copy, as
    # OPxFlasher does when flashing from the PC. Only those this ROM's vbmeta doesn't check at boot:
    # an old copy of a protected one (my_region, odm) would fail verification with the new vbmeta.
    from opx.rom.verify import avb_protected

    protected = avb_protected(rom)
    kept = []
    for it in items:
        # odm is build content (HALs, firmware config), never just region settings: always written
        if prof.is_conditional(it.name) and it.name != "odm" and it.name not in protected:
            it.keep_on_phone = True
            kept.append(it.name)
    items.sort(key=lambda i: (i.kind == "physical" and i.name in AVB_ORDER_LAST,
                              AVB_ORDER_LAST.index(i.name) if i.name in AVB_ORDER_LAST else 0))
    net_from_rom = [it.name for it in items
                    if prof.is_conditional(it.name) and it.name != "odm" and not it.keep_on_phone]
    p = Plan(items, skipped, kept=kept, net_from_rom=net_from_rom)
    p.boards = list(dict.fromkeys(b for b in [rom.codename, *prof.codenames, *prof.recovery_boards] if b))
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


RECOVERY_PARTS = ("recovery",)


def make_ready(rom: RomInfo, p: Plan, backup: Path, stage: Callable[[str], None]) -> None:
    """Convert ready: this phone's own network files (oplusstanvbk, my_region) go into the recovery
    zip from its backup, so one flash in recovery changes region and keeps mobile network. Only for
    the phone the backup was made on (the installer checks the serial). Files the ROM's vbmeta checks
    at boot always come from the ROM."""
    from opx.flash import backup_images
    from opx.rom.verify import avb_protected

    backup = Path(backup)
    try:
        manifest = json.loads((backup / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ConvertError(f"{backup} isn't an OPxFlasher backup ({e}).") from e
    dev = manifest.get("device", {})
    serial, model = str(dev.get("serial", "")), str(dev.get("model", ""))
    if not serial or not _SAFE.match(serial):
        raise ConvertError("The backup doesn't record the phone's serial number, so a convert ready zip couldn't "
                           "check it's flashed on the same phone.")
    prof = _profile(rom)
    protected = avb_protected(rom)
    slot = dev.get("slot_suffix", "")
    files = []
    for it in p.items:
        if not prof.is_conditional(it.name) or it.name == "odm":
            continue
        if it.name in protected:
            stage(f"{it.name} comes from the ROM: its vbmeta checks it at boot.")
            continue
        found = backup_images(backup, it.name)
        pick = next((found[n] for n in (it.name + slot, it.name, f"{it.name}_a", f"{it.name}_b") if n in found), None)
        if pick is None:
            raise ConvertError(f"The backup has no {it.name}. Make a Network essentials or Critical backup first.")
        path, sha, _src = pick
        if not path.is_file() or sha256_file(path) != sha:
            raise ConvertError(f"{path.name} in the backup doesn't match its recorded SHA-256: the backup is damaged.")
        it.backup_file, it.backup_sha, it.size, it.keep_on_phone = path, sha, path.stat().st_size, False
        files.append(it.name)
    # A recovery zip OPxFlasher made earlier left the phone's network files out (kept on the phone):
    # turning it into a convert ready zip adds them back, from the backup.
    have = {it.name for it in p.items}
    if rom.kind == "fullrom":
        for name in [n for n in prof.conditional if n != "odm" and n not in have and n not in protected]:
            found = backup_images(backup, name)
            hit = next(((n, found[n]) for n in (name + slot, name, f"{name}_a", f"{name}_b") if n in found), None)
            if hit is None:
                continue
            path, sha, src = hit[1]
            if not path.is_file() or sha256_file(path) != sha:
                raise ConvertError(f"{path.name} in the backup doesn't match its recorded SHA-256: the backup is damaged.")
            size = path.stat().st_size
            item = Item(name, "logical" if src == "mapper" else "physical", size,
                        RomImage(name=name, size=size, logical=src == "mapper"))
            item.backup_file, item.backup_sha = path, sha
            p.items.insert(0, item)  # before vbmeta (kept last)
            files.append(name)
    if not files:
        raise ConvertError("Nothing from the backup can go into this ROM: its vbmeta checks every network file.")
    p.kept = [n for n in p.kept if n not in files]
    p.ready = {"serial": serial, "model": model, "files": files}
    stage(f"Convert ready for {model or 'the backed-up phone'}: your {', '.join(files)} from the backup.")


def keep_recovery(p: Plan, stage: Callable[[str], None]) -> None:
    """Leave recovery out of the zip, so a custom recovery (AERA, OrangeFox) stays on the phone."""
    for it in p.items:
        if it.name in RECOVERY_PARTS:
            it.keep_on_phone = True
            p.recovery_kept = True
    if p.recovery_kept:
        stage("Recovery is left out: your custom recovery stays on the phone.")


def hardware_needed(rom: RomInfo) -> bool:
    """Does a recovery zip of this ROM have to know whether the phone is China hardware? (OnePlus 13, 17+:
    the ROM's own abl / engineering_cdt break fastboot on PJZ110 hardware, also after a conversion.)"""
    from opx import flash as fl

    try:
        return fl.needs_hardware_choice(_profile(rom), fl._major(rom.android))
    except (AttributeError, OSError, ValueError):
        return False  # not a ROM we can read the phone / version of: nothing China-specific to do


def keep_cn_boot_chain(rom: RomInfo, p: Plan, stage: Callable[[str], None]) -> None:
    """China hardware: leave the profile's cn_hardware_keep images (abl, engineering_cdt) out of the zip,
    so the phone keeps its own Android 16 ones."""
    keep = set(_profile(rom).cn_hardware_keep)
    for it in p.items:
        if it.name in keep:
            it.keep_on_phone = True
            p.cn_kept.append(it.name)
    if p.cn_kept:
        stage(f"China hardware: {', '.join(p.cn_kept)} are left out; the phone keeps its own (they must be from "
              "Android 16.0.3.501 or newer: flash the newest 16 first if it isn't on 16).")


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


_SUFFIX = re.compile(r"-fullrom(?:-[A-Z]{2,3})?(?:-ready-[A-Za-z0-9]+)?(?:-keeprec)?$")


def output_name(rom: RomInfo) -> str:
    stem = rom.path.name
    for ext in (".zip", ".bin"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
    if stem in ("payload", "OTA_FILES_HERE", "images") and rom.product:
        stem = f"{rom.product}_{rom.build_incremental or rom.android}"
    stem = _SUFFIX.sub("", stem)
    tag = rom_version_tag(rom)
    if tag and not has_version(stem, tag):
        stem += f"-{tag}"  # PJZ110_11.F.07_..._202610080010-17-0-1-202: the name says which build it is
    return re.sub(r"[^A-Za-z0-9._()+-]+", "_", stem).strip("_") or "rom"


def rom_version_tag(rom: RomInfo) -> str:
    """The ROM's OS version for file names ("17-0-1-202"), from its name or metadata; "" if unknown."""
    meta = rom.metadata or {}
    for text in (rom.path.name, meta.get("post-display-id", ""), meta.get("version_name", "")):
        tag = version_tag(text)
        if tag:
            return tag
    return ""


def output_paths(rom: RomInfo, out_dir: Path, ready_model: str = "", keep_rec: bool = False,
                 cn: bool = False) -> tuple[Path, Path]:
    """(recovery zip, image folder): <name>-fullrom[-REGION].zip and <name>-fullrom[-REGION].
    A convert ready zip (for one phone) adds -ready-<its model>; one that keeps the custom
    recovery adds -keeprec, so neither is mistaken for the plain zip."""
    region = region_code(rom)
    tail = f"-fullrom-{region}" if region else "-fullrom"
    base = output_name(rom) + tail
    zip_base = base
    if ready_model:
        zip_base += "-ready-" + (re.sub(r"[^A-Za-z0-9]", "", ready_model) or "phone")
    if keep_rec:
        zip_base += "-keeprec"
    if cn:
        zip_base += "-cnhw"
    return Path(out_dir) / f"{zip_base}.zip", Path(out_dir) / base


def existing_outputs(rom: RomInfo, out_dir: Path, make_zip: bool = True, make_folder: bool = False,
                     keep_rec: bool = False, hardware: str = "") -> dict[str, Path]:
    """Outputs convert() would make that are already there: {"zip": path, "folder": path}."""
    zip_out, folder_out = output_paths(rom, out_dir, keep_rec=keep_rec, cn=hardware == "cn")
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
                     cancel: Callable[[], bool] | None = None, keep_recovery_zip: bool = False,
                     hardware: str = "") -> dict[str, Path]:
    """Like convert(), but outputs that already exist are verified and reused, not refused.
    (A convert ready zip is always made fresh with convert(): it carries backup files.)"""
    have = existing_outputs(rom, out_dir, make_zip, make_folder, keep_recovery_zip, hardware)
    if have:
        (stage or (lambda s: None))("Already converted: verifying " + ", ".join(p.name for p in have.values()) + "...")
        verify_existing(have, progress, cancel)
    need_zip, need_folder = make_zip and "zip" not in have, make_folder and "folder" not in have
    made = (convert(rom, out_dir, need_zip, need_folder, progress, stage, cancel, keep_recovery_zip=keep_recovery_zip,
                    hardware=hardware)
            if need_zip or need_folder else {})
    return {**have, **made}


def convert(rom: RomInfo, out_dir: Path, make_zip: bool = True, make_folder: bool = False,
            progress: Progress | None = None, stage: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None, chunk: int = CHUNK, replace: bool = False,
            ready_backup: Path | None = None, keep_recovery_zip: bool = False,
            hardware: str = "") -> dict[str, Path]:
    """Write the full ROM. Returns {"zip": path, "folder": path} for what was made.
    Existing outputs are refused unless `replace` (then they're deleted first).
    `ready_backup`: make the recovery zip convert ready from this backup (see make_ready).
    `keep_recovery_zip`: the recovery zip leaves the phone's recovery alone.
    `hardware` ("cn" / "global"): needed for a recovery zip when `hardware_needed(rom)`; "cn" leaves the
    phone's own abl / engineering_cdt in place (China hardware). A convert ready zip from a PJZ backup is
    always China hardware."""
    if not (make_zip or make_folder):
        raise ConvertError("Choose a recovery zip, an image folder, or both.")
    if (ready_backup or keep_recovery_zip) and not make_zip:
        raise ConvertError("Convert ready and keeping the custom recovery are recovery zip options.")
    if ready_backup and make_folder:
        raise ConvertError("A convert ready zip is made on its own: it holds one phone's network files. "
                           "Make the image folder separately.")
    ready_model = ""
    if ready_backup:
        try:
            ready_model = json.loads((Path(ready_backup) / "manifest.json").read_text("utf-8"))["device"]["model"]
        except (OSError, ValueError, KeyError, TypeError):
            ready_model = "phone"
    if hardware not in ("", "cn", "global"):
        raise ConvertError(f"Unknown hardware {hardware!r}: use cn or global.")
    if ready_model.upper().startswith("PJZ"):
        if hardware == "global":
            raise ConvertError(f"The backup is from a {ready_model}: China hardware, also after a conversion. "
                               "Make the zip for China hardware.")
        if make_zip and hardware_needed(rom):
            hardware = "cn"
    if make_zip and not hardware and hardware_needed(rom):
        raise ConvertError(
            f"Android {rom.android or '17+'} recovery zips for the {_profile(rom).name} must know the phone's "
            "hardware: China hardware (PJZ110, also converted to Global) keeps its Android 16 abl and "
            "engineering_cdt, because this ROM's own break fastboot on it. Choose China or Global hardware "
            "(CLI: --hardware cn / global).")
    cn = make_zip and hardware == "cn" and bool(_profile(rom).cn_hardware_keep)
    if chunk <= 0 or chunk % (1 << 20):
        raise ConvertError("Parts must be whole MiB.")
    stage = stage or (lambda s: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_out, folder_out = output_paths(rom, out_dir, ready_model, keep_recovery_zip, cn)
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
            if ready_backup:
                make_ready(rom, p, Path(ready_backup), stage)
            if keep_recovery_zip:
                keep_recovery(p, stage)
                if hardware_needed(rom):
                    stage("Careful: a custom recovery made for Android 16 may not start with this ROM's boot "
                          "images (AERA for 16 didn't). Leaving the ROM's recovery in is safer.")
            if cn:
                keep_cn_boot_chain(rom, p, stage)
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
        if make_zip and p.kept:
            stage(f"Recovery zip keeps the phone's own {', '.join(p.kept)} (network / signal).")
        if make_folder and p.recovery_kept:
            stage("The image folder holds the ROM as it is (recovery included): when flashing from OPxFlasher, "
                  "tick Keep my custom recovery.")

        # one pass to extract / hash what's made, one more to pack what goes in the zip
        total = (p.total if make_folder else p.zip_total) + (p.zip_total if make_zip else 0)
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
                todo = [i for i in p.items if make_folder or not i.keep_on_phone]
                for n, item in enumerate(todo, 1):
                    stage(f"{item.name} ({n} of {len(todo)})")
                    img = item.image
                    direct = (item.backup_file is None and img.file is not None and not img.sparse
                              and not img.pieces and not make_folder)
                    if item.backup_file is not None:
                        path, sha = item.backup_file, item.backup_sha  # checked against the backup in make_ready
                        tick(item.name, item.size)
                    elif direct:
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
                    if zf is not None and not item.keep_on_phone:
                        rec["parts"] = _add_parts(zf, path, item, chunk, tick)
                        if item.backup_file is None and not direct and not make_folder:
                            path.unlink()  # a temporary copy; never the ROM's own file or the user's backup
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
                    left = {i.name for i in p.items if i.keep_on_phone}
                    in_zip = [r for r in records if r["name"] not in left]
                    zf.writestr("opx/rom.list", _install_list(rom, p, in_zip, chunk))
                    extra = {"convert_ready": {k: v for k, v in p.ready.items() if k != "serial"}} if p.ready else {}
                    if p.recovery_kept:
                        extra["recovery_kept"] = True
                    if p.cn_kept:
                        extra["china_hardware_kept"] = p.cn_kept
                    zf.writestr(FULLROM_JSON, json.dumps(dict(manifest, images=in_zip, kept_on_phone=p.kept, **extra),
                                                         indent=2))
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
    if p.ready:
        lines += [f"ready serial {p.ready['serial']}", f"ready model {p.ready['model'] or 'phone'}"]
        lines += [f"readyfile {n}" for n in p.ready["files"]]
    if p.recovery_kept:
        lines.append("keeprecovery")
    lines += [f"cnkeep {n}" for n in p.cn_kept]  # China hardware: the phone's own (16) abl / engineering_cdt
    lines += [f"kept {n}" for n in p.kept]  # shown at the end: the phone's own network files stayed
    lines += [f"netrom {n}" for n in p.net_from_rom]  # ... or the ROM wrote its own
    for r in records:
        lines.append(f"image {r['kind']} {r['name']} {r['size']} {r['sha256']}")
        # worked out here: a recovery's shell may only count to 2^31, and odm alone is bigger
        lines.append(f"blocks {r['name']} {-(-r['size'] // (1 << 20))} {-(-r['size'] // 512)}")
        for i, pt in enumerate(r["parts"]):
            lines.append(f"part {r['name']} {i} {pt['entry']} {pt['sha256']} {pt['size']}")
    return "\n".join(lines) + "\n"
