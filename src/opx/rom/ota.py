"""Recognising a firmware package and describing what is inside it."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx.rom import payload as pl
from opx.rom import sparse

SMALL_IMAGE_LIMIT = 64 * 1024 * 1024
FULLROM_JSON = "opxflasher-rom.json"
ENCRYPTED = {".ofp": "an OPPO factory package (.ofp)", ".ops": "a OnePlus MSM download package (.ops)",
             ".ozip": "an encrypted OPPO OTA (.ozip)"}
# names of dynamic (logical) partitions, for image sets that don't say which are which
_LOGICAL = re.compile(r"^(system|system_ext|system_dlkm\w*|product|vendor|vendor_dlkm|odm|odm_dlkm|"
                      r"my_\w+|mi_ext)$")
_SUPER_PIECE = re.compile(r"^super(?:[._-]?img)?[._-](\d+)(?:\.img)?$|^super_(\d+)$|^super\.(\d+)$")


class RomError(ValueError):
    pass


@dataclass
class RomImage:
    name: str
    size: int  # raw (unsparsed) size
    logical: bool | None  # None = unknown (fastboot folders)
    file: Path | None = None  # set for loose .img files
    members: list[str] = field(default_factory=list)  # zip entries (several = parts, in order)
    sparse: bool = False  # stored as an Android sparse image
    sha256: str = ""  # of the raw image, when the package records it
    pieces: list[Path] = field(default_factory=list)  # sparse pieces of one image (split super)
    in_super: object = None  # lp.Partition, for a logical partition inside the ROM's super.img
    guessed: bool = False  # logical/physical decided from the name only


@dataclass
class RomInfo:
    path: Path
    kind: str  # "ota-zip", "payload", "folder", "image-zip", "fullrom"
    metadata: dict[str, str] = field(default_factory=dict)
    payload: pl.Payload | None = None
    images: list[RomImage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    super_raw: Path | None = None  # super.img expanded to a raw file (converter / flashing)

    # --- facts --------------------------------------------------------------

    @property
    def fingerprint(self) -> str:
        return self.metadata.get("post-build", "")

    @property
    def codename(self) -> str:
        if self.metadata.get("pre-device"):
            return self.metadata["pre-device"].split(",")[0]
        return _fingerprint_part(self.fingerprint, "device")

    @property
    def product(self) -> str:
        return _fingerprint_part(self.fingerprint, "product")

    @property
    def android(self) -> str:
        return _fingerprint_part(self.fingerprint, "release")

    @property
    def security_patch(self) -> str:
        return self.metadata.get("post-security-patch-level") or (self.payload.security_patch if self.payload else "")

    @property
    def build_incremental(self) -> str:
        return self.metadata.get("post-build-incremental", "")

    @property
    def is_full(self) -> bool | None:
        if self.kind == "backup":
            return True  # a Full firmware backup is a complete image set
        if self.payload is not None:
            return self.payload.is_full  # decided by the data (see Payload.is_full)
        if "pre-build" in self.metadata or "pre-build-incremental" in self.metadata:
            return False
        return None if self.kind not in ("folder", "image-zip", "fullrom") else True

    def image(self, name: str) -> RomImage | None:
        for img in self.images:
            if img.name == name:
                return img
        return None

    def read_image(self, name: str) -> bytes:
        """Return a small image's bytes (vbmeta, xbl_config, abl ...)."""
        img = self.image(name)
        if img is None:
            raise RomError(f"{name} is not in this ROM")
        if img.size > SMALL_IMAGE_LIMIT:
            raise RomError(f"{name} is too large to read into memory")
        if img.file is not None and not img.sparse:
            return img.file.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            return materialize(self, name, Path(tmp) / f"{name}.img").read_bytes()


def _fingerprint_part(fp: str, part: str) -> str:
    # brand/product/device:release/id/incremental:type/tags
    try:
        left, rest = fp.split(":", 1)
        brand, product, device = left.split("/")
        release = rest.split("/")[0]
    except ValueError:
        return ""
    return {"product": product, "device": device, "release": release}[part]


def parse_metadata(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _from_payload(info: RomInfo, source: pl.ByteSource) -> None:
    payload = pl.parse(source)
    info.payload = payload
    logical = payload.logical_names
    info.images = [
        RomImage(name=p.name, size=p.size, logical=(p.name in logical) if logical else None)
        for p in payload.partitions
    ]
    if not payload.is_full:
        info.notes.append("This is an incremental OTA (" + (payload.incremental_reason or "no partitions")
                          + "). Only full OTAs can be flashed or converted.")
        return
    labels = []
    if "pre-build" in info.metadata or "pre-build-incremental" in info.metadata:
        labels.append("its metadata names a build it updates from")
    if payload.minor_version != 0:
        labels.append(f"the payload is marked as delta format (minor version {payload.minor_version})")
    if any(p.source_info for p in payload.partitions):
        labels.append("some partitions name the version they replace")
    if labels:
        info.notes.append("Full package: " + " and ".join(labels) + ", but every partition carries complete "
                          "data (each image is checked against its SHA-256).")


def open_rom(path: str | Path) -> RomInfo:
    path = Path(path).expanduser()
    if not path.exists():
        raise RomError(f"{path} does not exist")

    if path.is_file() and path.suffix.lower() in ENCRYPTED:
        raise RomError(f"This is {ENCRYPTED[path.suffix.lower()]}. It's encrypted with OPPO's keys, which "
                       "OPxFlasher doesn't have. Use a full OTA zip of the same build instead (Official ROMs).")
    if path.is_file() and _head(path, 12) == b"OPPOENCRYPT!":
        raise RomError(f"This is {ENCRYPTED['.ozip']}. Use a full OTA zip of the same build instead (Official ROMs).")

    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if FULLROM_JSON in names:
                return _fullrom_zip(path, zf)
            payloads = [n for n in names if n == "payload.bin" or n.endswith("/payload.bin")]
            if payloads:
                where = min(payloads, key=len)
                info = RomInfo(path=path, kind="ota-zip")
                meta = where[: -len("payload.bin")] + "META-INF/com/android/metadata"
                for m in (meta, "META-INF/com/android/metadata"):
                    if m in names:
                        info.metadata = parse_metadata(zf.read(m).decode("utf-8", "replace"))
                        break
                if where != "payload.bin":
                    info.notes.append(f"payload.bin is in the folder '{where[:-12]}' of this zip.")
            elif any(n.lower().endswith(".img") for n in names):
                return _image_zip(path, zf, names)
            else:
                inner = [n for n in names if n.lower().endswith(".zip")]
                hint = f" It holds {inner[0]}: unzip it and choose that file." if len(inner) == 1 else ""
                raise RomError("This zip has no payload.bin or images, so it isn't a ROM OPxFlasher can read." + hint)
        _from_payload(info, pl.ByteSource.from_zip_member(path, where))
        return info

    if path.is_file() and sparse.is_sparse(path):
        raise RomError("This is a single sparse image, not a ROM. Use Flash image for one partition, "
                       "or choose the folder it's in.")
    if path.is_file() and _head(path, 4) != b"CrAU":
        if path.suffix.lower() == ".img":
            raise RomError("This is a single partition image, not a ROM. Use Flash image for it, "
                           "or choose the folder it's in.")
        raise RomError("OPxFlasher doesn't recognise this file as a ROM (full OTA zip, payload.bin, "
                       "image folder or image zip).")
    if path.is_file():
        info = RomInfo(path=path, kind="payload")
        _from_payload(info, pl.ByteSource(path))
        return info

    # folder: an extracted OTA (payload.bin) or loose fastboot images
    meta = path / "META-INF" / "com" / "android" / "metadata"
    info = RomInfo(path=path, kind="folder")
    if meta.exists():
        info.metadata = parse_metadata(meta.read_text("utf-8", "replace"))
    if (path / "payload.bin").exists():
        info.kind = "payload"
        _from_payload(info, pl.ByteSource(path / "payload.bin"))
        return info
    if (path / FULLROM_JSON).exists():
        return _fullrom_folder(path)
    files = [f for f in sorted(path.iterdir()) if f.is_file() and _looks_like_image(f.name)]
    if not files and (path / "images").is_dir():
        files = [f for f in sorted((path / "images").iterdir()) if f.is_file() and _looks_like_image(f.name)]
    if not files:
        raise RomError("No payload.bin or .img files found in this folder.")
    info.images = _folder_images(files, info.notes)
    _classify(info)
    if not info.metadata:
        _identity_from_avb(info)
    if not info.metadata:
        info.notes.append("No OTA metadata in this folder, so device and build can't be confirmed from the ROM.")
    return info


def _head(path: Path, n: int) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _looks_like_image(name: str) -> bool:
    low = name.lower()
    return low.endswith(".img") or bool(_SUPER_PIECE.match(low))


def _folder_images(files: list[Path], notes: list[str]) -> list[RomImage]:
    images, pieces = [], []
    for f in files:
        if _SUPER_PIECE.match(f.name.lower()) and f.name.lower() != "super.img":
            pieces.append(f)
            continue
        is_sp = sparse.is_sparse(f)
        size = sparse.raw_size(f) if is_sp else f.stat().st_size
        images.append(RomImage(name=f.stem, size=size, logical=None, file=f, sparse=is_sp))
    if pieces:
        if any(i.name == "super" for i in images):
            raise RomError("This folder has both super.img and split super pieces; keep only one.")
        if not all(sparse.is_sparse(p) for p in pieces):
            raise RomError("The split super pieces aren't sparse images, so they can't be joined.")
        sizes = {sparse.raw_size(p) for p in pieces}
        if len(sizes) != 1:
            raise RomError("The split super pieces describe different sizes; the set is incomplete or mixed.")
        pieces.sort(key=lambda p: int(next(g for g in _SUPER_PIECE.match(p.name.lower()).groups() if g)))
        images.append(RomImage(name="super", size=sizes.pop(), logical=False, pieces=pieces, sparse=True))
        notes.append(f"super is split into {len(pieces)} sparse pieces; OPxFlasher joins them.")
    return images


def _classify(info: RomInfo) -> None:
    """Which images are logical (inside super)? From super.img's own table when the
    set has one (raw), else from the usual partition names."""
    sup = info.image("super")
    if sup is not None and sup.file is not None and not sup.sparse:
        from opx import lp

        try:
            md = lp.read_super_file(sup.file)
        except (lp.LpError, OSError) as e:
            info.notes.append(f"super.img's partition table can't be read ({e}).")
            md = None
        if md is not None:
            slots = {lp.strip_slot(p.name)[1] for p in md.partitions if p.size}
            slot = "_a" if "_a" in slots else next(iter(slots), "")
            for p in md.partitions:
                base, sfx = lp.strip_slot(p.name)
                if p.size and sfx == slot and info.image(base) is None:
                    info.images.append(RomImage(name=base, size=p.size, logical=True, in_super=p))
            info.notes.append(f"Logical partitions read from super.img (slot {slot.strip('_') or 'none'}).")
    for img in info.images:
        if img.logical is None:
            img.logical = bool(_LOGICAL.match(img.name)) and img.name != "super"
            img.guessed = True


def _image_zip(path: Path, zf: zipfile.ZipFile, names: list[str]) -> RomInfo:
    """A zip of fastboot images (flasher kit, AERA image zip): read in place."""
    info = RomInfo(path=path, kind="image-zip")
    imgs = [n for n in names if _looks_like_image(n.rsplit("/", 1)[-1]) and not n.endswith("/")]
    folders: dict[str, list[str]] = {}
    for n in imgs:
        folders.setdefault(n.rsplit("/", 1)[0] if "/" in n else "", []).append(n)
    where = max(folders, key=lambda k: len(folders[k]))
    if len(folders) > 1:
        info.notes.append(f"Using the {len(folders[where])} images in '{where or 'the top folder'}' of this zip.")
    scripts = [n for n in names if n.lower().endswith((".bat", ".sh"))]
    if scripts:
        info.notes.append("This is a fastboot flasher package; OPxFlasher reads its images directly "
                          "(its scripts are not run).")
    if "android-info.txt" in names:
        info.notes.append("This looks like an AERA / fastboot image zip (android-info.txt).")
    for n in sorted(folders[where]):
        base = n.rsplit("/", 1)[-1]
        if _SUPER_PIECE.match(base.lower()) and base.lower() != "super.img":
            raise RomError("This zip has super split into pieces; unzip it and choose the folder.")
        zi = zf.getinfo(n)
        with zf.open(n) as fh:
            is_sp = sparse.is_sparse_bytes(fh.read(4))
        size = zi.file_size
        if is_sp:
            with zf.open(n) as fh:
                _, _, blk, total, _ = sparse.header(fh)
            size = blk * total
        name = base[:-4] if base.lower().endswith(".img") else base
        info.images.append(RomImage(name=name, size=size, logical=None, members=[n], sparse=is_sp))
    sup = info.image("super")
    if sup is not None:
        info.notes.append("This zip has a super.img; its logical partitions are read when it's flashed or converted.")
    _classify(info)
    _identity_from_avb(info)
    if not info.metadata:
        info.notes.append("No OTA metadata in this zip, so device and build can't be confirmed from the ROM.")
    return info


def _fullrom_manifest(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
        if data.get("format") != "opxflasher-fullrom" or not isinstance(data.get("images"), list):
            raise ValueError
        return data
    except (ValueError, AttributeError) as e:
        raise RomError(f"{FULLROM_JSON} is damaged or from a newer OPxFlasher.") from e


def _fullrom_images(data: dict, folder: Path | None) -> list[RomImage]:
    out = []
    for item in data["images"]:
        img = RomImage(name=item["name"], size=int(item["size"]), logical=item.get("kind") == "logical",
                       sha256=item.get("sha256", ""), guessed=item.get("kind") == "auto")
        if folder is not None:
            img.file = folder / item["file"]
        else:
            img.members = [p["entry"] for p in item["parts"]]
        out.append(img)
    return out


def _fullrom_zip(path: Path, zf: zipfile.ZipFile) -> RomInfo:
    data = _fullrom_manifest(zf.read(FULLROM_JSON))
    info = RomInfo(path=path, kind="fullrom", metadata=dict(data.get("metadata", {})))
    info.images = _fullrom_images(data, None)
    names = set(zf.namelist())
    missing = [m for i in info.images for m in i.members if m not in names]
    if missing:
        raise RomError(f"This full-ROM zip is incomplete ({missing[0]} is missing).")
    info.notes.append(f"OPxFlasher full ROM, converted from {data.get('source', {}).get('name', 'a ROM')}.")
    return info


def _fullrom_folder(path: Path) -> RomInfo:
    data = _fullrom_manifest((path / FULLROM_JSON).read_bytes())
    info = RomInfo(path=path, kind="fullrom", metadata=dict(data.get("metadata", {})))
    info.images = _fullrom_images(data, path)
    missing = [i.file.name for i in info.images if not i.file.exists()]
    if missing:
        raise RomError(f"This full-ROM folder is incomplete ({missing[0]} is missing).")
    info.notes.append(f"OPxFlasher full ROM, converted from {data.get('source', {}).get('name', 'a ROM')}.")
    return info


# --- getting an image out ------------------------------------------------------


def materialize(rom: RomInfo, name: str, target: Path,
                progress: Callable[[int], None] | None = None, verify: bool = True) -> Path:
    """Write image `name` to `target` as a plain (raw, unsparsed) file, whatever the
    ROM stores it as. Checks the recorded SHA-256 when the ROM has one."""
    img = rom.image(name)
    if img is None:
        raise RomError(f"{name} is not in this ROM")
    target = Path(target)
    partial = target.with_name(target.name + ".part")
    tick = progress or (lambda n: None)
    try:
        if rom.payload is not None and img.file is None and not img.members:
            pl.extract(rom.payload, name, partial)
            tick(img.size)
        elif img.in_super is not None:
            from opx import lp

            sup = rom.image("super")
            src = _raw_super(rom, sup, target.parent)
            with open(src, "rb") as sf, open(partial, "wb") as out:
                lp.copy_partition(sf, img.in_super, _Counting(out, tick))
        elif img.pieces:
            with open(partial, "wb") as out:
                out.truncate(img.size)
                for piece in img.pieces:
                    sparse.expand(piece, out, tick, overlay=True)
        elif img.file is not None:
            if img.sparse:
                with open(partial, "wb") as out:
                    sparse.expand(img.file, out, tick)
            else:
                with open(img.file, "rb") as src, open(partial, "wb") as out:
                    _copy(src, out, tick)
        elif img.members:
            with zipfile.ZipFile(rom.path) as zf, open(partial, "wb") as out:
                if img.sparse:
                    with zf.open(img.members[0]) as src:
                        sparse.expand(src, out, tick)
                else:
                    for m in img.members:
                        with zf.open(m) as src:
                            _copy(src, out, tick)
        else:
            raise RomError(f"{name} has no data in this ROM")
        size = partial.stat().st_size
        if size != img.size:
            raise RomError(f"{name}: got {size} bytes, expected {img.size}")
        if verify and img.sha256 and sha256_file(partial) != img.sha256:
            raise RomError(f"{name} doesn't match the SHA-256 recorded in the package; it's damaged.")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


def _raw_super(rom: RomInfo, sup: RomImage, workdir: Path) -> Path:
    if rom.super_raw is not None and rom.super_raw.exists():
        return rom.super_raw
    if sup.file is not None and not sup.sparse and not sup.pieces:
        return sup.file
    cached = workdir / "super.raw.img"
    if not (cached.exists() and cached.stat().st_size == sup.size):
        materialize(rom, "super", cached)
    rom.super_raw = cached
    return cached


class _Counting:
    def __init__(self, out, tick):
        self.out, self.tick = out, tick

    def write(self, data):
        self.out.write(data)
        self.tick(len(data))


def _copy(src, out, tick) -> None:
    while chunk := src.read(1 << 20):
        out.write(chunk)
        tick(len(chunk))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# brands of chip-platform (not phone) builds; their device field is the SoC platform (sun, canoe ...)
GENERIC_BRANDS = {"qti", "qcom", "android", "generic", "mediatek", "mtk", "unisoc", "sprd", "aosp"}


def is_generic_fingerprint(fp: str) -> bool:
    return fp.split("/", 1)[0].strip().lower() in GENERIC_BRANDS


# AVB properties the build system writes, e.g. com.android.build.system.fingerprint
_AVB_PROP = re.compile(r"com\.android\.build\.(\w+)\.(fingerprint|security_patch)")


def _identity_from_avb(info: RomInfo) -> None:
    """No OTA metadata (loose images): take the device and build from the signed AVB
    properties in vbmeta_system / vbmeta_vendor / vbmeta. Gives up (fail closed) when
    they disagree about the device."""
    from opx.rom import avb

    found: dict[str, dict[str, str]] = {"fingerprint": {}, "security_patch": {}}
    sources: dict[str, set[str]] = {}
    source = ""
    for name in ("vbmeta_system", "vbmeta_vendor", "vbmeta"):
        img = info.image(name)
        if img is None or img.size > 1 << 20 or (img.file is None and not img.members):
            continue
        try:
            vb = avb.parse(info.read_image(name))
        except (OSError, RomError, sparse.SparseError, avb.AvbError):
            continue
        for key, value in vb.properties.items():
            m = _AVB_PROP.fullmatch(key)
            if m and value:
                found[m.group(2)].setdefault(m.group(1), value)
                source = source or name
                sources.setdefault(name, set()).add(m.group(1))
    fps = found["fingerprint"]
    if not fps:
        return
    # chip-platform builds (qti/canoe/canoe...) are shared by many phones: never an identity
    oem = {part: fp for part, fp in fps.items() if not is_generic_fingerprint(fp)}
    if not oem:
        platforms = sorted({_fingerprint_part(fp, "device") for fp in fps.values()} - {""})
        info.notes.append(f"The images' signed build data only names the chip platform ({', '.join(platforms)}), "
                          "which many phones share, so the phone and build can't be confirmed from the ROM.")
        return
    devices = {_fingerprint_part(fp, "device") for fp in oem.values()} - {""}
    if len(devices) != 1:
        info.notes.append("The images' signed build data names more than one device "
                          f"({', '.join(sorted(devices)) or 'none'}), so the device can't be confirmed.")
        return
    fps = oem
    source = next((n for n in ("vbmeta_vendor", "vbmeta_system", "vbmeta") if n in sources and sources[n] & set(oem)),
                  source)
    fp = next((oem[k] for k in ("vendor", "odm", "product", "system") if k in oem), next(iter(oem.values())))
    info.metadata["post-build"] = fp
    # brand/product/device:release/id/incremental:type/tags
    incremental = fp.split(":")[1].split("/")[-1] if fp.count(":") >= 2 else ""
    if incremental:
        info.metadata["post-build-incremental"] = incremental
    patches = found["security_patch"]
    patch = next((patches[k] for k in ("vendor", "odm", "product", "system") if k in patches and k in fps),
                 next((v for k, v in patches.items() if k in fps), ""))
    if patch:
        info.metadata["post-security-patch-level"] = patch
    info.notes.append(f"No OTA metadata: device and build read from the signed AVB data in {source} ({fp}).")
