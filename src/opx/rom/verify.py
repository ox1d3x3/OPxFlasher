"""Verify ROM: is this package complete, intact and usable, and with what?

Works on anything OPxFlasher can open (full OTA zip, payload.bin, image folder,
image zip, OPxFlasher full ROM). Nothing is written to the phone or the ROM.

- Integrity: every byte is checked against what the package itself records:
  payload.bin against payload_properties.txt (or every operation's SHA-256),
  OPxFlasher full ROMs against their SHA-256 list, zip entries against their CRC,
  and boot-type images against the digests in the ROM's own vbmeta.
- Completeness: the partitions a full ROM needs, and everything the ROM's vbmeta
  chain says should be there.
- Sanity: image headers (boot, vendor_boot, vbmeta, dtbo, sparse), logical
  images at least as large as their verified data, verification not disabled.
- Compatibility: OPxFlasher (check and flash), OrangeFox / AERA (recovery zip),
  and whether converting would make it usable.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx.rom import avb, sparse
from opx.rom import payload as pl
from opx.rom.detect import Detection, detect
from opx.rom.ota import FULLROM_JSON, RomError, RomInfo, materialize

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
YES, CONVERT, NO = "yes", "convert", "no"

# images whose first bytes are fixed; a wrong one there means a wrong or damaged file
MAGIC = {
    "boot": (b"ANDROID!",), "init_boot": (b"ANDROID!",), "recovery": (b"ANDROID!",),
    "vendor_boot": (b"VNDRBOOT",), "vendor_kernel_boot": (b"VNDRBOOT",),
    "vbmeta": (b"AVB0",), "vbmeta_system": (b"AVB0",), "vbmeta_vendor": (b"AVB0",),
    "dtbo": (b"\xd7\xb7\xab\x1e",),
}
# Qualcomm signed firmware (ELF); a mismatch is a warning (formats vary between chips)
ELF = ("abl", "xbl", "xbl_config", "tz", "hyp", "aop", "devcfg", "keymaster", "qupfw", "uefisecapp")
HASH_LIMIT = 512 << 20  # hash-descriptor images are boot-sized; anything bigger isn't checked that way
CHUNK = 1 << 20


def _size(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


class VerifyCancelled(Exception):
    pass


@dataclass
class Finding:
    status: str  # ok / warn / fail / info
    area: str
    text: str


@dataclass
class Target:
    name: str
    status: str  # yes / convert / no
    text: str


@dataclass
class VerifyReport:
    path: Path
    detection: Detection
    findings: list[Finding] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    images: list[tuple[str, int, str]] = field(default_factory=list)  # name, size, kind
    deep: bool = True

    def add(self, status: str, area: str, text: str) -> None:
        self.findings.append(Finding(status, area, text))

    @property
    def failed(self) -> bool:
        return any(f.status == FAIL for f in self.findings)

    @property
    def verdict(self) -> str:
        if self.failed:
            return "FAILED"
        return "VERIFIED WITH WARNINGS" if any(f.status == WARN for f in self.findings) else "VERIFIED"

    @property
    def needs_conversion(self) -> bool:
        return any(t.status == CONVERT for t in self.targets)

    def sorted_findings(self) -> list[Finding]:
        order = {FAIL: 0, WARN: 1, OK: 2, INFO: 3}
        return sorted(self.findings, key=lambda f: order.get(f.status, 9))

    def lines(self) -> list[str]:
        mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", INFO: "i   "}
        tmark = {YES: "yes", CONVERT: "after converting", NO: "no"}
        out = [f"Verify: {self.path}", f"Result: {self.verdict}" + ("" if self.deep else " (quick check)"), ""]
        out += [f"  [{mark[f.status]}] {f.area:<16} {f.text}" for f in self.sorted_findings()]
        out.append("")
        out += [f"  {t.name:<20} {tmark[t.status]:<17} {t.text}" for t in self.targets]
        return out


Progress = Callable[[str, int, int], None]  # (what, done, total)


class _Meter:
    def __init__(self, progress: Progress | None, cancel: Callable[[], bool] | None):
        self.progress, self.cancel = progress, cancel

    def tick(self, what: str, done: int, total: int) -> None:
        if self.cancel and self.cancel():
            raise VerifyCancelled
        if self.progress:
            self.progress(what, done, total)


def verify(path: str | Path, deep: bool = True, progress: Progress | None = None,
           cancel: Callable[[], bool] | None = None) -> VerifyReport:
    """Check a ROM. `deep` reads every byte (minutes for a 9 GB OTA); quick checks
    structure, headers and completeness only."""
    meter = _Meter(progress, cancel)
    d = detect(path)
    rep = VerifyReport(Path(path), d, deep=deep)
    if not d.ok:
        rep.add(FAIL, "Package", f"Not a ROM OPxFlasher can read: {d.problem}")
        rep.targets = [Target("OPxFlasher", NO, d.problem), Target("OrangeFox / AERA", NO, d.problem)]
        return rep
    rom = d.rom
    rep.images = [(i.name, i.size, "logical" if i.logical else "physical" if i.logical is False else "?")
                  for i in rom.images]

    _package(rep, rom, d)
    with tempfile.TemporaryDirectory(prefix="opx-verify-") as tmp:
        work = Path(tmp)
        if deep:
            _integrity(rep, rom, meter)
        else:
            rep.add(INFO, "Integrity", "Quick check: the data itself wasn't read. Run the full check before flashing.")
        _headers(rep, rom, work)
        _avb_checks(rep, rom, work, deep, meter)
    _targets(rep, rom, d)
    return rep


# --- package and completeness ---------------------------------------------------


def _package(rep: VerifyReport, rom: RomInfo, d: Detection) -> None:
    rep.add(INFO, "Type", d.label)
    if d.full is False:
        rep.add(FAIL, "Package", "Incremental OTA: it only holds changes against one older build. "
                                 "Use the full OTA of the build you want.")
    elif d.full is None:
        rep.add(WARN, "Package", "Can't tell whether this is a complete package.")
    else:
        rep.add(OK, "Package", f"Full package, {len(d.physical) + len(d.logical)} images, {_size(d.total_size)}.")
    if rom.codename and rom.product:
        rep.add(OK, "Identity", f"For {rom.product} ({rom.codename}), Android {rom.android or '?'}, "
                                f"patch {rom.security_patch or '?'}" + (f", {d.build}" if d.build else "") + ".")
    else:
        rep.add(WARN, "Identity", "The ROM doesn't say which phone it's for. OPxFlasher can still compare it with "
                                  "your phone, but a recovery zip can't be made from it.")
    if d.missing:
        rep.add(FAIL, "Completeness", f"Missing {', '.join(d.missing)}: a full ROM always has these.")
    else:
        rep.add(OK, "Completeness", "Has the boot, vbmeta and system images a full ROM needs.")
    if d.skipped:
        rep.add(INFO, "Protected", f"Never written by OPxFlasher: {', '.join(d.skipped)} (identity / personal data).")
    if d.guessed:
        rep.add(INFO, "Layout", "Which images live inside super is guessed from their names (no OTA metadata).")
    for note in rom.notes:
        rep.add(INFO, "Note", note)


# --- integrity: every byte against what the package records ----------------------


def _integrity(rep: VerifyReport, rom: RomInfo, meter: _Meter) -> None:
    try:
        if rom.payload is not None:
            _payload_integrity(rep, rom, meter)
        if rom.kind == "fullrom":
            _fullrom_integrity(rep, rom, meter)
        if rom.path.is_file() and zipfile.is_zipfile(rom.path):
            _zip_integrity(rep, rom, meter)
        if rom.kind in ("folder",) and rom.payload is None:
            _sparse_integrity(rep, rom, meter)
    except VerifyCancelled:
        raise
    except (OSError, RomError, pl.PayloadError, zipfile.BadZipFile) as e:
        rep.add(FAIL, "Integrity", f"Reading the ROM failed: {e}")


def _payload_properties(rom: RomInfo) -> dict[str, str]:
    text = ""
    if rom.path.is_file() and zipfile.is_zipfile(rom.path):
        with zipfile.ZipFile(rom.path) as zf:
            names = zf.namelist()
            props = [n for n in names if n == "payload_properties.txt" or n.endswith("/payload_properties.txt")]
            if props:
                text = zf.read(min(props, key=len)).decode("utf-8", "replace")
    else:
        f = Path(rom.payload.source.path).with_name("payload_properties.txt")
        if f.exists():
            text = f.read_text("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _payload_integrity(rep: VerifyReport, rom: RomInfo, meter: _Meter) -> None:
    src = rom.payload.source
    props = _payload_properties(rom)
    try:
        want = base64.b64decode(props.get("FILE_HASH", ""), validate=True)
    except ValueError:
        want = b""
    size = int(props["FILE_SIZE"]) if props.get("FILE_SIZE", "").isdigit() else None
    if len(want) == 32:
        if size is not None and size != src.size:
            rep.add(FAIL, "Integrity", f"payload.bin is {src.size} bytes; the package says {size}. It's truncated or damaged.")
            return
        h = hashlib.sha256()
        with open(src.path, "rb") as fh:
            fh.seek(src.base)
            done = 0
            while done < src.size:
                chunk = fh.read(min(CHUNK * 8, src.size - done))
                if not chunk:
                    break
                h.update(chunk)
                done += len(chunk)
                meter.tick("payload.bin", done, src.size)
        if done != src.size or h.digest() != want:
            rep.add(FAIL, "Integrity", "payload.bin doesn't match the SHA-256 in payload_properties.txt: "
                                       "the download is damaged. Download it again.")
        else:
            rep.add(OK, "Integrity", f"payload.bin matches its SHA-256 ({_size(src.size)} checked).")
        return
    # no package hash: check every operation's data instead
    total = sum(op.data_length for p in rom.payload.partitions for op in p.operations())
    done, unhashed = 0, 0
    for part in rom.payload.partitions:
        for i, op in enumerate(part.operations(), 1):
            if not op.data_length:
                continue
            raw = src.read_at(rom.payload.data_start + op.data_offset, op.data_length)
            if op.data_sha256 is None:
                unhashed += 1
            elif hashlib.sha256(raw).digest() != op.data_sha256:
                rep.add(FAIL, "Integrity", f"{part.name}: data block {i} is damaged (hash mismatch). Download it again.")
                return
            done += op.data_length
            meter.tick(part.name, done, total)
    if unhashed:
        rep.add(WARN, "Integrity", f"{unhashed} data blocks carry no hash; the rest ({_size(done)}) match.")
    else:
        rep.add(OK, "Integrity", f"Every data block of payload.bin matches its SHA-256 ({_size(done)} checked).")


def _fullrom_integrity(rep: VerifyReport, rom: RomInfo, meter: _Meter) -> None:
    total = sum(i.size for i in rom.images)
    done = 0
    zf = zipfile.ZipFile(rom.path) if rom.path.is_file() else None
    try:
        for img in rom.images:
            if not img.sha256:
                rep.add(WARN, "Integrity", f"{img.name} has no recorded SHA-256.")
                continue
            h = hashlib.sha256()
            streams = [zf.open(m) for m in img.members] if zf else [open(img.file, "rb")]
            for s in streams:
                with s:
                    while chunk := s.read(CHUNK * 4):
                        h.update(chunk)
                        done += len(chunk)
                        meter.tick(img.name, done, total)
            if h.hexdigest() != img.sha256:
                rep.add(FAIL, "Integrity", f"{img.name} doesn't match its recorded SHA-256: the ROM is damaged.")
                return
    finally:
        if zf:
            zf.close()
    rep.add(OK, "Integrity", f"Every image matches its recorded SHA-256 ({_size(done)} checked).")


def _zip_integrity(rep: VerifyReport, rom: RomInfo, meter: _Meter) -> None:
    """CRC of every zip entry (payload.bin is covered by its own hash above)."""
    with zipfile.ZipFile(rom.path) as zf:
        entries = [i for i in zf.infolist() if not i.is_dir() and not i.filename.endswith("payload.bin")]
        if rom.kind == "fullrom":  # images were read (and CRC-checked) by the SHA-256 pass
            covered = {m for img in rom.images for m in img.members}
            entries = [i for i in entries if i.filename not in covered]
        total = sum(i.file_size for i in entries) or 1
        done = 0
        for info in entries:
            with zf.open(info) as fh:  # zipfile raises BadZipFile on a CRC mismatch at the end
                while chunk := fh.read(CHUNK * 4):
                    done += len(chunk)
                    meter.tick(info.filename, done, total)
    if entries:
        rep.add(OK, "Zip", f"All {len(entries)} other zip entries pass their CRC check.")


def _sparse_integrity(rep: VerifyReport, rom: RomInfo, meter: _Meter) -> None:
    """Loose images have no recorded hashes; at least make sure sparse ones expand cleanly."""
    bad = []
    for img in rom.images:
        if img.file is not None and img.sparse:
            try:
                with open(img.file, "rb") as fh:
                    if sparse.check(fh) != img.size:
                        raise sparse.SparseError("expands to the wrong size")
            except (sparse.SparseError, OSError) as e:
                bad.append(f"{img.name} ({e})")
            meter.tick(img.name, 0, 0)
    if bad:
        rep.add(FAIL, "Sparse", "Damaged sparse image(s): " + "; ".join(bad))
    elif any(i.sparse for i in rom.images):
        rep.add(OK, "Sparse", "Sparse images are well formed.")
    rep.add(INFO, "Integrity", "Loose images don't record their own hashes; boot-type images are checked "
                               "against vbmeta below, the rest by their headers and sizes.")


# --- headers ---------------------------------------------------------------------


def _head(rom: RomInfo, name: str, n: int = 16) -> bytes | None:
    """First bytes of an image without unpacking big ones; None when that would be costly."""
    img = rom.image(name)
    if img is None or img.sparse or img.pieces or img.in_super is not None:
        return None
    if img.file is not None:
        with open(img.file, "rb") as fh:
            return fh.read(n)
    if img.members:
        with zipfile.ZipFile(rom.path) as zf, zf.open(img.members[0]) as fh:
            return fh.read(n)
    return None  # inside payload.bin: covered by the payload's own hashes


def _headers(rep: VerifyReport, rom: RomInfo, work: Path) -> None:
    if rom.payload is not None:
        return  # payload images are reconstructed and hash-checked; no loose files to mislabel
    wrong, elf_wrong, checked = [], [], 0
    for img in rom.images:
        head = _head(rom, img.name)
        if head is None:
            continue
        if img.name in MAGIC:
            checked += 1
            if not head.startswith(MAGIC[img.name]):
                wrong.append(img.name)
        elif img.name in ELF:
            checked += 1
            if not head.startswith(b"\x7fELF"):
                elf_wrong.append(img.name)
    if wrong:
        rep.add(FAIL, "Headers", f"{', '.join(wrong)}: not the kind of image the name says (wrong or damaged file).")
    if elf_wrong:
        rep.add(WARN, "Headers", f"{', '.join(elf_wrong)}: not a signed firmware (ELF) file as expected.")
    if checked and not wrong and not elf_wrong:
        rep.add(OK, "Headers", f"{checked} boot / vbmeta / firmware images have the right headers.")


# --- AVB: completeness and digests from the ROM's own vbmeta ---------------------


def _vbmeta(rom: RomInfo, name: str) -> avb.Vbmeta | None:
    if rom.image(name) is None:
        return None
    try:
        return avb.parse(rom.read_image(name))
    except (avb.AvbError, RomError, pl.PayloadError, OSError):
        return None


def avb_protected(rom: RomInfo) -> set[str]:
    """Partitions this ROM's AVB chain checks at boot (hash, hash tree or chained vbmeta).
    Keeping the phone's old copy of one of these while flashing this ROM's vbmeta would fail
    verification, unless it's the very same build. Unknown (no readable vbmeta) -> empty set."""
    top = _vbmeta(rom, "vbmeta")
    if top is None:
        return set()
    tables = [top] + [vb for c, _ in top.chains if (vb := _vbmeta(rom, c)) is not None]
    for img in rom.images:  # images carrying their own vbmeta (AVB footer) protect themselves
        if img.file is not None and not img.sparse:
            try:
                own = avb.parse_file_footer(img.file)
            except (avb.AvbError, OSError):
                own = None
            if own is not None:
                tables.append(own)
    out = {c for c, _ in top.chains}
    for vb in tables:
        out |= set(vb.hash_partitions) | set(vb.hashtree_partitions)
    return out


def _avb_checks(rep: VerifyReport, rom: RomInfo, work: Path, deep: bool, meter: _Meter) -> None:
    top = _vbmeta(rom, "vbmeta")
    if top is None:
        if rom.image("vbmeta") is not None:
            rep.add(FAIL, "AVB", "vbmeta can't be read: it's damaged or not a vbmeta image.")
        return
    tables = [("vbmeta", top)]
    for chained, _loc in top.chains:
        vb = _vbmeta(rom, chained)
        if vb is not None:
            tables.append((chained, vb))
    if top.verification_disabled or top.hashtree_disabled:
        rep.add(WARN, "AVB", "This vbmeta has verification turned off (a modified ROM, not stock).")

    # loose image files often carry their own vbmeta in an AVB footer (system.img, boot.img ...)
    for img in rom.images:
        if img.file is not None and not img.sparse and img.name not in dict(tables):
            try:
                own = avb.parse_file_footer(img.file)
            except (avb.AvbError, OSError):
                own = None
            if own is not None:
                tables.append((img.name, own))

    expected = {c for c, _ in top.chains}
    hashes: dict[str, avb.HashDescriptor] = {}
    trees: dict[str, avb.HashtreeDescriptor] = {}
    sizes: dict[str, int] = {}
    for _name, vb in tables:
        trees.update({t.partition: t for t in vb.hashtrees})
        expected |= set(vb.hash_partitions) | set(vb.hashtree_partitions)
        hashes.update({h.partition: h for h in vb.hashes})
        sizes.update(vb.hashtree_sizes)
    have = {i.name for i in rom.images}
    missing = sorted(expected - have)
    if missing:
        rep.add(WARN, "AVB", f"vbmeta also covers {', '.join(missing)}, which this ROM doesn't include; "
                             "the phone keeps its own copies of those.")
    elif expected:
        rep.add(OK, "AVB", f"Every partition vbmeta covers is in the ROM ({len(expected)}).")

    small = [n for n, s in sizes.items() if rom.image(n) is not None and rom.image(n).size < s]
    if small:
        rep.add(FAIL, "AVB", f"{', '.join(small)}: smaller than the data vbmeta protects (truncated image).")

    if not deep:
        return
    checked, bad = 0, []
    for name, desc in sorted(hashes.items()):
        img = rom.image(name)
        if img is None or desc.image_size > img.size or img.size > HASH_LIMIT:
            continue
        algo = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}.get(desc.algorithm)
        if algo is None:
            continue
        meter.tick(f"{name} (vbmeta digest)", checked, len(hashes))
        try:
            data = _image_bytes(rom, name, work)
        except (RomError, pl.PayloadError, OSError) as e:
            bad.append(f"{name} (unreadable: {e})")
            continue
        if algo(desc.salt + data[: desc.image_size]).digest() != desc.digest:
            bad.append(name)
        checked += 1
    if bad:
        rep.add(FAIL, "AVB digests", f"{', '.join(bad)} don't match the digest in vbmeta: damaged, or modified "
                                     "(for example patched for root). A stock ROM must match.")
    elif checked:
        rep.add(OK, "AVB digests", f"{checked} boot-type images match the digests in vbmeta.")
    _hashtrees(rep, rom, trees, work, meter)


class _ZipMember:
    """One zip entry as a stream; closes the zip with it."""

    def __init__(self, path: Path, member: str):
        self.zf = zipfile.ZipFile(path)
        self.fh = self.zf.open(member)
        self.read = self.fh.read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()
        self.zf.close()


def _hashtrees(rep: VerifyReport, rom: RomInfo, trees: dict[str, avb.HashtreeDescriptor], work: Path,
               meter: _Meter) -> None:
    """dm-verity, the way the phone checks system / vendor / ... at boot: rebuild each image's hash
    tree and compare the root. Only for loose images; payloads and OPxFlasher full ROMs are already
    covered byte for byte by their own hashes."""
    if rom.payload is not None or rom.kind == "fullrom":
        return
    todo = [(n, t) for n, t in sorted(trees.items())
            if (img := rom.image(n)) is not None and img.in_super is None and not img.pieces and t.image_size <= img.size]
    total = sum(t.image_size for _n, t in todo)
    done, checked, bad, skipped = 0, 0, [], []
    for name, desc in todo:
        img = rom.image(name)
        temp = None
        try:
            if img.file is not None and not img.sparse:
                fh = open(img.file, "rb")
            elif img.members and len(img.members) == 1 and not img.sparse:
                fh = _ZipMember(rom.path, img.members[0])
            else:
                temp = materialize(rom, name, work / f"{name}.img", verify=False)
                fh = open(temp, "rb")
            with fh:
                base = done
                root = avb.hashtree_root(fh.read, desc, tick=lambda d, _t, base=base, name=name: meter.tick(
                    f"{name} (dm-verity)", base + d, total))
            if root != desc.root_digest:
                bad.append(name)
            checked += 1
        except avb.AvbError as e:
            skipped.append(f"{name} ({e})")
        except (RomError, pl.PayloadError, OSError, zipfile.BadZipFile) as e:
            bad.append(f"{name} (unreadable: {e})")
        finally:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)
        done += desc.image_size
    if bad:
        rep.add(FAIL, "dm-verity", f"{', '.join(bad)}: the data doesn't match its hash tree in vbmeta: damaged or "
                                   "modified. The phone would refuse it (or it only works with verity off).")
    elif checked:
        rep.add(OK, "dm-verity", f"{checked} system-type images match their hash trees ({_size(total)} checked).")
    if skipped:
        rep.add(INFO, "dm-verity", "Not checked: " + "; ".join(skipped))


def _image_bytes(rom: RomInfo, name: str, work: Path) -> bytes:
    img = rom.image(name)
    if img.file is not None and not img.sparse:
        return img.file.read_bytes()
    out = materialize(rom, name, work / f"{name}.img", verify=False)
    try:
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


# --- what can use it ---------------------------------------------------------------


def _recovery_zip_ready(rom: RomInfo) -> bool:
    if rom.kind != "fullrom" or not rom.path.is_file():
        return False
    with zipfile.ZipFile(rom.path) as zf:
        names = set(zf.namelist())
    return {"META-INF/com/google/android/update-binary", FULLROM_JSON} <= names


def _targets(rep: VerifyReport, rom: RomInfo, d: Detection) -> None:
    if rep.failed:
        why = next(f.text for f in rep.sorted_findings() if f.status == FAIL)
        rep.targets = [Target("OPxFlasher", NO, why), Target("OrangeFox / AERA", NO, why)]
        return
    rep.targets.append(Target(
        "OPxFlasher", YES,
        "Check ROM compares it with your phone, then Flash writes it." if d.flashable else "Not flashable."))
    if _recovery_zip_ready(rom):
        rep.targets.append(Target("OrangeFox / AERA", YES, "Flash this zip in the recovery; its installer checks "
                                                           "the phone and the build before writing."))
        return
    from opx import convert as cv

    ok, why = d.convertible
    if ok:
        try:
            p = cv.plan(rom) if not (rom.image("super") and not any(i.in_super for i in rom.images)) else None
            recovery_ok, problem = (p.recovery_ok, p.recovery_problem) if p else (True, "")
        except cv.ConvertError as e:
            recovery_ok, problem = False, str(e)
        if recovery_ok:
            rep.targets.append(Target("OrangeFox / AERA", CONVERT,
                                      "Convert to full ROM makes a recovery zip with a checked installer."))
        else:
            rep.targets.append(Target("OrangeFox / AERA", NO, f"A recovery zip can't be made: {problem}."))
    else:
        rep.targets.append(Target("OrangeFox / AERA", NO, why))
