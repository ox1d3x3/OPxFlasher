"""A custom recovery (AERA, OrangeFox, TWRP ...) the owner downloaded from its official page.

OPxFlasher doesn't download or ship custom recoveries: the profile links to the official page
(`custom_recoveries`), the owner downloads it there and loads the file here. A zip is opened and its one
recovery image found by what it is (an Android boot image), not by its name. The SHA-256 of the chosen file is
shown so the owner can compare it with the one on the download page; the image is then written like any other
single image (opx.single: recovery, both slots).
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from opx.single import BOOT

MAX_IMAGE = 256 * 1024 * 1024  # recovery partitions are ~100 MB; anything far bigger isn't a recovery image
MIN_IMAGE = 1024 * 1024
OUT_NAME = "custom-recovery.img"


class CustomRecError(ValueError):
    pass


@dataclass
class Loaded:
    source: Path  # what the owner chose (zip or img)
    source_sha256: str  # compare this with the download page
    image: Path  # the recovery image to flash
    image_sha256: str
    member: str = ""  # the image's name inside the zip ("" when an .img was chosen)

    @property
    def from_zip(self) -> bool:
        return bool(self.member)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def normalise_sha(text: str) -> str:
    """The hash out of whatever was pasted ("SHA-256: ABC...", with spaces...), lower case; "" if none."""
    runs = re.findall(r"[0-9a-f]+", re.sub(r"[\s:]", "", (text or "").lower()))
    exact = [r for r in runs if len(r) == 64]
    if exact:
        return exact[0]
    # "SHA256:abc..." runs the label's digits into the hash: the hash is the end of the run
    near = [r for r in runs if 64 < len(r) <= 67]
    return near[0][-64:] if near else ""


def matches(loaded: Loaded, pasted: str) -> bool | None:
    """None: nothing (readable) pasted. True / False: it matches the chosen file or not."""
    want = normalise_sha(pasted)
    if not want:
        return None
    return want in (loaded.source_sha256, loaded.image_sha256)


def _is_boot_image(head: bytes) -> bool:
    return head.startswith(BOOT)


def _candidates(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    found = []
    for info in zf.infolist():
        if info.is_dir() or not (MIN_IMAGE <= info.file_size <= MAX_IMAGE):
            continue
        try:
            with zf.open(info) as fh:
                if _is_boot_image(fh.read(8)):
                    found.append(info)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
            continue  # encrypted / unsupported member: not ours
    return found


def _pick(found: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    if len(found) == 1:
        return found[0]
    named = [i for i in found if Path(i.filename).name.lower().startswith("recovery")]
    if len(named) == 1:
        return named[0]
    if not found:
        raise CustomRecError("This zip has no recovery image in it. Choose the recovery zip from the official "
                             "download page (or the recovery .img inside it).")
    names = ", ".join(i.filename for i in found)
    raise CustomRecError(f"This zip has several images ({names}) and OPxFlasher can't tell which is the "
                         "recovery. Extract the recovery .img yourself and choose that.")


def load(path: Path, workdir: Path) -> Loaded:
    """Check the chosen file and return the image to flash (a zip's image is extracted into workdir)."""
    path = Path(path)
    if not path.is_file():
        raise CustomRecError(f"File not found: {path}")
    source_sha = sha256_of(path)
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                info = _pick(_candidates(zf))
                workdir.mkdir(parents=True, exist_ok=True)
                out = workdir / OUT_NAME
                written = 0
                with zf.open(info) as src, open(out, "wb") as dst:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        written += len(block)
                        if written > MAX_IMAGE:
                            raise CustomRecError("The recovery image in this zip is far too big.")
                        dst.write(block)
        except zipfile.BadZipFile as e:
            raise CustomRecError(f"The zip is damaged: {e}. Download it again.") from e
        return Loaded(path, source_sha, out, sha256_of(out), info.filename)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(8)
    if not _is_boot_image(head):
        raise CustomRecError("This file isn't a recovery image (or a zip with one). Choose the file from the "
                             "official download page.")
    if not (MIN_IMAGE <= size <= MAX_IMAGE):
        raise CustomRecError(f"This image is {size:,} bytes: that's not the size of a recovery image.")
    return Loaded(path, source_sha, path, source_sha)


def pages(profile) -> list[dict[str, str]]:
    return list(getattr(profile, "custom_recoveries", []) or [])


def all_pages() -> list[tuple[str, dict[str, str]]]:
    """(phone name, {name, url}) for every built-in profile that names a custom recovery."""
    from opx import profile as pf

    out = []
    for pid in pf.all_profile_ids():
        try:
            p = pf.load(pid)
        except Exception:  # noqa: BLE001 - a broken user profile shouldn't hide the others
            continue
        out += [(p.name, r) for r in p.custom_recoveries]
    return out
