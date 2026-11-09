"""Recognising a firmware package and describing what is inside it."""

from __future__ import annotations

import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from opx.rom import payload as pl

SMALL_IMAGE_LIMIT = 64 * 1024 * 1024


class RomError(ValueError):
    pass


@dataclass
class RomImage:
    name: str
    size: int
    logical: bool | None  # None = unknown (fastboot folders)
    file: Path | None = None  # set for loose .img files


@dataclass
class RomInfo:
    path: Path
    kind: str  # "ota-zip", "payload", "folder"
    metadata: dict[str, str] = field(default_factory=dict)
    payload: pl.Payload | None = None
    images: list[RomImage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
        if "pre-build" in self.metadata or "pre-build-incremental" in self.metadata:
            return False
        if self.payload is not None:
            return self.payload.is_full
        return None if self.kind != "folder" else True

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
        if img.file is not None:
            return img.file.read_bytes()
        assert self.payload is not None
        with tempfile.TemporaryDirectory() as tmp:
            out = pl.extract(self.payload, name, Path(tmp) / f"{name}.img")
            return out.read_bytes()


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
        info.notes.append("This is an incremental OTA. Only full OTAs can be flashed safely.")


def open_rom(path: str | Path) -> RomInfo:
    path = Path(path).expanduser()
    if not path.exists():
        raise RomError(f"{path} does not exist")

    if path.is_file() and zipfile.is_zipfile(path):
        info = RomInfo(path=path, kind="ota-zip")
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "META-INF/com/android/metadata" in names:
                info.metadata = parse_metadata(zf.read("META-INF/com/android/metadata").decode("utf-8", "replace"))
            if "payload.bin" not in names:
                raise RomError("This zip has no payload.bin; it is not an A/B OTA package.")
        _from_payload(info, pl.ByteSource.from_zip_member(path, "payload.bin"))
        return info

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
    files = sorted(path.glob("*.img")) or sorted((path / "images").glob("*.img"))
    if not files:
        raise RomError("No payload.bin or .img files found in this folder.")
    info.images = [RomImage(name=f.stem, size=f.stat().st_size, logical=None, file=f) for f in files]
    if not info.metadata:
        info.notes.append("No OTA metadata in this folder, so device and build can't be confirmed from the ROM.")
    return info
