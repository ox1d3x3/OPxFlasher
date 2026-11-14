"""Android sparse images (what `img2simg` / fastboot use): recognise and expand.

Format (AOSP system/core/libsparse, little-endian), written clean-room:

    file header (28 bytes): magic 0xED26FF3A, major 1, minor 0, file_hdr_sz,
        chunk_hdr_sz, blk_sz, total_blks, total_chunks, image_checksum
    chunk header (12 bytes): type, reserved, chunk_sz (blocks), total_sz (bytes, incl. header)
        0xCAC1 raw        data follows (chunk_sz * blk_sz bytes)
        0xCAC2 fill       4-byte pattern repeated
        0xCAC3 don't care nothing written (left as zeros / a hole)
        0xCAC4 crc32      4 bytes, ignored

Recoveries write images with `dd`, which can't take sparse files, so the
converter expands them. Several sparse files can also be pieces of one image
(e.g. super.img split by fastboot); `expand(..., overlay=True)` lays each one
over the same output.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

MAGIC = 0xED26FF3A
RAW, FILL, DONT_CARE, CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4
_HEAD = struct.Struct("<IHHHHIIII")
_CHUNK = struct.Struct("<HHII")


class SparseError(ValueError):
    pass


def is_sparse(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return len(head) == 4 and struct.unpack("<I", head)[0] == MAGIC


def is_sparse_bytes(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack("<I", head[:4])[0] == MAGIC


def header(fh: BinaryIO) -> tuple[int, int, int, int, int]:
    """(file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks)."""
    raw = fh.read(_HEAD.size)
    if len(raw) < _HEAD.size:
        raise SparseError("sparse image is truncated")
    magic, major, _minor, fhs, chs, blk, total_blks, chunks, _ = _HEAD.unpack(raw)
    if magic != MAGIC or major != 1 or fhs < _HEAD.size or chs < _CHUNK.size or blk == 0 or blk % 4:
        raise SparseError("not a valid Android sparse image")
    fh.read(fhs - _HEAD.size)
    return fhs, chs, blk, total_blks, chunks


def raw_size(path: Path) -> int:
    with open(path, "rb") as fh:
        _, _, blk, total_blks, _ = header(fh)
    return blk * total_blks


def expand(src: Path | BinaryIO, out: BinaryIO, progress: Callable[[int], None] | None = None,
           overlay: bool = False) -> int:
    """Write the full image to `out` (opened for writing, positioned anywhere).
    `src` is a path or a file object read front to back (a zip entry works).
    Don't-care chunks are skipped, so `out` must start as zeros (a new file
    truncated to size, or an earlier piece when `overlay`). Returns the size."""
    if isinstance(src, (str, Path)):
        with open(src, "rb") as fh:
            return _expand(fh, Path(src).name, out, progress, overlay)
    return _expand(src, getattr(src, "name", "image"), out, progress, overlay)


def _expand(fh: BinaryIO, label: str, out: BinaryIO, progress, overlay: bool) -> int:
    src = Path(label)
    _, chs, blk, total_blks, chunks = header(fh)
    size = blk * total_blks
    if not overlay:
        out.truncate(size)
    block = 0
    for i in range(chunks):
        raw = fh.read(_CHUNK.size)
        if len(raw) < _CHUNK.size:
            raise SparseError(f"{src.name}: chunk {i + 1} is truncated")
        ctype, _, nblocks, total = _CHUNK.unpack(raw)
        fh.read(chs - _CHUNK.size)
        body = total - chs
        if block + nblocks > total_blks and ctype != CRC32:
            raise SparseError(f"{src.name}: chunk {i + 1} runs past the end of the image")
        if ctype == RAW:
            if body != nblocks * blk:
                raise SparseError(f"{src.name}: raw chunk {i + 1} has the wrong length")
            out.seek(block * blk)
            left = body
            while left:
                data = fh.read(min(left, 1 << 20))
                if not data:
                    raise SparseError(f"{src.name}: raw chunk {i + 1} is truncated")
                out.write(data)
                left -= len(data)
                if progress:
                    progress(len(data))
        elif ctype == FILL:
            pattern = fh.read(4)
            if body != 4 or len(pattern) != 4:
                raise SparseError(f"{src.name}: fill chunk {i + 1} is malformed")
            out.seek(block * blk)
            if pattern != b"\0\0\0\0" or overlay:
                piece = pattern * (blk // 4) * 256
                left = nblocks * blk
                while left:
                    n = min(left, len(piece))
                    out.write(piece[:n])
                    left -= n
            if progress:
                progress(nblocks * blk)
        elif ctype == DONT_CARE:
            if body:
                raise SparseError(f"{src.name}: don't-care chunk {i + 1} carries data")
        elif ctype == CRC32:
            fh.read(body)
            continue
        else:
            raise SparseError(f"{src.name}: unknown chunk type {ctype:#x}")
        block += nblocks
    return size


class _Discard:
    """A write target that keeps nothing: walks a sparse image without writing it."""

    def truncate(self, n):
        return n

    def seek(self, n, whence=0):
        return n

    def write(self, data):
        return len(data)


def check(fh: BinaryIO) -> int:
    """Read a whole sparse image and check every chunk; nothing is written. Returns the raw size."""
    return _expand(fh, getattr(fh, "name", "image"), _Discard(), None, False)
