"""Reading Android A/B ``payload.bin`` files (update_engine format).

Supports listing partitions and extracting images from **full** OTAs
(REPLACE, REPLACE_BZ, REPLACE_XZ, ZERO, DISCARD, and REPLACE_ZSTD when the
optional ``zstandard`` package is installed). Incremental payloads are
detected and refused for extraction, since they need the old images.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import struct
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx.rom import protobuf as pb

MAGIC = b"CrAU"

# InstallOperation.Type (update_metadata.proto)
REPLACE, REPLACE_BZ, MOVE, BSDIFF, SOURCE_COPY, SOURCE_BSDIFF = 0, 1, 2, 3, 4, 5
ZERO, DISCARD, REPLACE_XZ, PUFFDIFF, BROTLI_BSDIFF, ZUCCHINI = 6, 7, 8, 9, 10, 11
LZ4DIFF_BSDIFF, LZ4DIFF_PUFFDIFF, REPLACE_ZSTD = 12, 13, 14
FULL_OPS = {REPLACE, REPLACE_BZ, REPLACE_XZ, ZERO, DISCARD, REPLACE_ZSTD}
OP_NAMES = {
    0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF", 4: "SOURCE_COPY", 5: "SOURCE_BSDIFF",
    6: "ZERO", 7: "DISCARD", 8: "REPLACE_XZ", 9: "PUFFDIFF", 10: "BROTLI_BSDIFF", 11: "ZUCCHINI",
    12: "LZ4DIFF_BSDIFF", 13: "LZ4DIFF_PUFFDIFF", 14: "REPLACE_ZSTD",
}


class PayloadError(ValueError):
    pass


# --- byte sources -----------------------------------------------------------


class ByteSource:
    """Random access to a payload stored in a plain file or inside a zip."""

    def __init__(self, path: Path, base: int = 0, size: int | None = None, label: str = ""):
        self.path = Path(path)
        self.base = base
        self.size = size if size is not None else self.path.stat().st_size - base
        self.label = label or str(path)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > self.size:
            raise PayloadError("read past the end of the payload (file truncated?)")
        with open(self.path, "rb") as fh:
            fh.seek(self.base + offset)
            data = fh.read(length)
        if len(data) != length:
            raise PayloadError("short read from payload")
        return data

    @classmethod
    def from_zip_member(cls, zip_path: Path, member: str) -> ByteSource:
        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo(member)
        if info.compress_type != zipfile.ZIP_STORED:
            raise PayloadError(f"{member} is compressed inside the zip; extract the zip first.")
        with open(zip_path, "rb") as fh:
            fh.seek(info.header_offset)
            header = fh.read(30)
        if header[:4] != b"PK\x03\x04":
            raise PayloadError("bad zip local header")
        name_len, extra_len = struct.unpack("<HH", header[26:30])
        base = info.header_offset + 30 + name_len + extra_len
        return cls(zip_path, base=base, size=info.file_size, label=f"{zip_path}!{member}")


# --- manifest ---------------------------------------------------------------


@dataclass
class Operation:
    type: int
    data_offset: int
    data_length: int
    dst_extents: list[tuple[int, int]]
    data_sha256: bytes | None


@dataclass
class PartitionUpdate:
    name: str
    size: int
    sha256: bytes | None
    incremental: bool  # some operation needs the old image (delta data)
    version: str
    _ops_raw: list[bytes] = field(repr=False, default_factory=list)
    source_info: bool = False  # carries old_partition_info (informational: data may still be complete)
    delta_op: str = ""  # first operation that needs the old image, for messages

    def operations(self) -> list[Operation]:
        ops = []
        for raw in self._ops_raw:
            f = pb.decode(raw)
            extents = []
            for ext in f.get(6, []):
                e = pb.decode(ext)
                extents.append((pb.first(e, 1, 0), pb.first(e, 2, 0)))
            ops.append(
                Operation(
                    type=pb.first(f, 1, 0),
                    data_offset=pb.first(f, 2, 0),
                    data_length=pb.first(f, 3, 0),
                    dst_extents=extents,
                    data_sha256=pb.first(f, 8),
                )
            )
        return ops


@dataclass
class DynamicGroup:
    name: str
    size: int
    partitions: list[str]


@dataclass
class Payload:
    source: ByteSource
    version: int
    block_size: int
    minor_version: int
    max_timestamp: int | None
    security_patch: str
    partitions: list[PartitionUpdate]
    groups: list[DynamicGroup]
    data_start: int

    @property
    def is_full(self) -> bool:
        """Every partition carries complete data (no operation needs the old image).
        That is what makes images extractable; each one is still checked against its
        SHA-256. Labels (minor_version, empty old_partition_info) don't decide it."""
        return bool(self.partitions) and not any(p.incremental for p in self.partitions)

    @property
    def incremental_reason(self) -> str:
        bad = [p for p in self.partitions if p.incremental]
        if not bad:
            return ""
        shown = ", ".join(f"{p.name} ({p.delta_op})" for p in bad[:3]) + (f" and {len(bad) - 3} more" if len(bad) > 3 else "")
        return f"{len(bad)} of {len(self.partitions)} partitions only hold changes against the old version: {shown}"

    @property
    def logical_names(self) -> set[str]:
        return {n for g in self.groups for n in g.partitions}

    def partition(self, name: str) -> PartitionUpdate:
        for p in self.partitions:
            if p.name == name:
                return p
        raise PayloadError(f"{name} is not in this payload")


def parse(source: ByteSource) -> Payload:
    head = source.read_at(0, 24 if source.size >= 24 else source.size)
    if head[:4] != MAGIC:
        raise PayloadError("not an update_engine payload (missing CrAU header)")
    version, manifest_size = struct.unpack(">QQ", head[4:20])
    if version == 1:
        header_size, sig_size = 20, 0
    elif version == 2:
        header_size = 24
        (sig_size,) = struct.unpack(">I", head[20:24])
    else:
        raise PayloadError(f"unsupported payload version {version}")
    if manifest_size > 64 * 1024 * 1024:
        raise PayloadError("manifest is implausibly large")
    manifest = pb.decode(source.read_at(header_size, manifest_size))

    partitions = []
    for raw in manifest.get(13, []):
        f = pb.decode(raw)
        new_info = pb.decode(pb.first(f, 7, b""))
        ops_raw = f.get(8, [])
        old = pb.decode(pb.first(f, 6, b"")) if f.get(6) else {}
        source_info = bool(pb.first(old, 1, 0) or pb.first(old, 2))  # a real old size / hash
        delta_op = ""
        for op in ops_raw:
            kind = pb.first(pb.decode(op), 1, 0)
            if kind not in FULL_OPS:
                delta_op = OP_NAMES.get(kind, f"unknown operation {kind}")
                break
        incremental = bool(delta_op)
        partitions.append(
            PartitionUpdate(
                name=pb.text(f, 1),
                size=pb.first(new_info, 1, 0),
                sha256=pb.first(new_info, 2),
                incremental=incremental,
                version=pb.text(f, 15),
                _ops_raw=ops_raw,
                source_info=source_info,
                delta_op=delta_op,
            )
        )

    groups = []
    dyn = pb.first(manifest, 15)
    if dyn:
        for raw in pb.decode(dyn).get(1, []):
            g = pb.decode(raw)
            groups.append(
                DynamicGroup(
                    name=pb.text(g, 1),
                    size=pb.first(g, 2, 0),
                    partitions=[v.decode() for v in g.get(3, [])],
                )
            )

    return Payload(
        source=source,
        version=version,
        block_size=pb.first(manifest, 3, 4096),
        minor_version=pb.first(manifest, 12, 0),
        max_timestamp=pb.first(manifest, 14),
        security_patch=pb.text(manifest, 18),
        partitions=partitions,
        groups=groups,
        data_start=header_size + manifest_size + sig_size,
    )


# --- extraction -------------------------------------------------------------


def _decompress(op_type: int, data: bytes) -> bytes:
    if op_type == REPLACE:
        return data
    if op_type == REPLACE_BZ:
        return bz2.decompress(data)
    if op_type == REPLACE_XZ:
        return lzma.decompress(data)
    if op_type == REPLACE_ZSTD:
        try:
            import zstandard  # optional
        except ImportError as exc:
            raise PayloadError("this payload uses zstd; run 'pip install zstandard'") from exc
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    raise PayloadError(f"operation {OP_NAMES.get(op_type, op_type)} needs the old image (incremental OTA)")


def extract(payload: Payload, name: str, out_path: Path, progress: Callable[[int, int], None] | None = None) -> Path:
    part = payload.partition(name)
    if part.incremental:
        raise PayloadError(f"{name} is incremental in this payload and can't be extracted on its own")
    bs = payload.block_size
    out_path = Path(out_path)
    partial = out_path.with_suffix(out_path.suffix + ".part")
    ops = part.operations()
    try:
        # Any failure closes the file before it is removed: Windows can't
        # delete a file that is still open.
        with open(partial, "wb") as fh:
            fh.truncate(part.size)
            for i, op in enumerate(ops, 1):
                total_blocks = sum(n for _, n in op.dst_extents)
                if op.type in (ZERO, DISCARD):
                    for start, num in op.dst_extents:
                        fh.seek(start * bs)
                        fh.write(b"\0" * (num * bs))
                else:
                    raw = payload.source.read_at(payload.data_start + op.data_offset, op.data_length)
                    if op.data_sha256 and hashlib.sha256(raw).digest() != op.data_sha256:
                        raise PayloadError(f"{name}: operation {i} data is corrupt (hash mismatch)")
                    data = _decompress(op.type, raw)
                    if len(data) != total_blocks * bs:
                        raise PayloadError(
                            f"{name}: operation {i} produced {len(data)} bytes, expected {total_blocks * bs}")
                    pos = 0
                    for start, num in op.dst_extents:
                        if (start + num) * bs > part.size:
                            raise PayloadError(f"{name}: operation {i} writes past the end of the partition")
                        fh.seek(start * bs)
                        fh.write(data[pos : pos + num * bs])
                        pos += num * bs
                if progress:
                    progress(i, len(ops))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    if part.sha256:
        h = hashlib.sha256()
        with open(partial, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        if h.digest() != part.sha256:
            partial.unlink(missing_ok=True)
            raise PayloadError(f"{name}: extracted image does not match the payload's hash")
    partial.replace(out_path)
    return out_path
