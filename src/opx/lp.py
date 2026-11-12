"""Android dynamic-partition ("liblp") metadata: read it from a super image,
cut out logical partitions, and write a metadata-only image (super_empty.img).

Format (AOSP system/core/fs_mgr/liblp, all little-endian):

    super image:  4096 reserved bytes | geometry (4096) | backup geometry (4096)
                  | primary metadata x slot_count | backup metadata x slot_count | data
    image file:   geometry (4096) | one metadata copy          (what lpmake writes)

    geometry:  magic 0x616c4467, struct_size 52, sha256, metadata_max_size,
               metadata_slot_count, logical_block_size
    metadata:  header (128 bytes for v10.0/10.1, 256 for v10.2) then tables:
               partitions (52 B), extents (24 B), groups (48 B), block devices (64 B)

Written clean-room from the published format; checksums are SHA-256 with the
checksum field zeroed.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

RESERVED = 4096
GEOMETRY_SIZE = 4096
SECTOR = 512
GEOMETRY_MAGIC = 0x616C4467
HEADER_MAGIC = 0x414C5030
ATTR_READONLY = 0x1
EXTENT_LINEAR, EXTENT_ZERO = 0, 1

_GEOM = struct.Struct("<II32sIII")            # 52 bytes
_HEAD = struct.Struct("<IHHI32sI32s12I")      # 128 bytes (v10.0 / v10.1)
_PART = struct.Struct("<36sIIII")             # 52
_EXT = struct.Struct("<QIQI")                 # 24
_GROUP = struct.Struct("<36sIQ")              # 48
_BDEV = struct.Struct("<QIIQ36sI")            # 64


class LpError(ValueError):
    pass


def _name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


@dataclass
class Extent:
    num_sectors: int
    target_type: int = EXTENT_LINEAR
    target_data: int = 0  # first physical sector for LINEAR
    target_source: int = 0


@dataclass
class Partition:
    name: str
    attributes: int
    group: int
    extents: list[Extent] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(e.num_sectors for e in self.extents) * SECTOR


@dataclass
class Group:
    name: str
    flags: int
    maximum_size: int


@dataclass
class BlockDevice:
    first_logical_sector: int
    alignment: int
    alignment_offset: int
    size: int
    name: str
    flags: int


@dataclass
class Metadata:
    metadata_max_size: int
    slot_count: int
    block_size: int
    major: int = 10
    minor: int = 2
    flags: int = 0
    partitions: list[Partition] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    block_devices: list[BlockDevice] = field(default_factory=list)

    def partition(self, name: str) -> Partition | None:
        return next((p for p in self.partitions if p.name == name), None)


# --------------------------------------------------------------------- read


def _parse_geometry(buf: bytes) -> tuple[int, int, int]:
    magic, size, checksum, max_size, slots, block = _GEOM.unpack_from(buf, 0)
    if magic != GEOMETRY_MAGIC or size != _GEOM.size:
        raise LpError("not dynamic-partition metadata (bad geometry)")
    zeroed = _GEOM.pack(magic, size, b"\0" * 32, max_size, slots, block)
    if hashlib.sha256(zeroed).digest() != checksum:
        raise LpError("geometry checksum mismatch")
    if not (0 < slots <= 8) or max_size % SECTOR or not max_size:
        raise LpError("unexpected geometry values")
    return max_size, slots, block


def _parse_metadata(buf: bytes, max_size: int, slots: int, block: int) -> Metadata:
    head = _HEAD.unpack_from(buf, 0)
    magic, major, minor, header_size, h_sum, tables_size, t_sum = head[:7]
    descs = head[7:]
    if magic != HEADER_MAGIC or major != 10 or minor > 2:
        raise LpError("unsupported metadata header")
    if header_size != (256 if minor >= 2 else 128):
        raise LpError("unexpected metadata header size")
    header = bytearray(buf[:header_size])
    header[12:44] = b"\0" * 32
    if hashlib.sha256(header).digest() != h_sum:
        raise LpError("metadata header checksum mismatch")
    tables = buf[header_size:header_size + tables_size]
    if len(tables) != tables_size or hashlib.sha256(tables).digest() != t_sum:
        raise LpError("metadata tables checksum mismatch")
    flags = struct.unpack_from("<I", buf, 128)[0] if minor >= 2 else 0

    def table(i, st):
        off, num, esize = descs[i * 3:(i + 1) * 3]
        if esize != st.size or off + num * esize > tables_size:
            raise LpError("bad metadata table")
        return [st.unpack_from(tables, off + n * esize) for n in range(num)]

    parts_raw, ext_raw, groups_raw, bdev_raw = (table(0, _PART), table(1, _EXT), table(2, _GROUP), table(3, _BDEV))
    extents = [Extent(*e) for e in ext_raw]
    md = Metadata(max_size, slots, block, major, minor, flags)
    for name, attrs, first, count, group in parts_raw:
        if first + count > len(extents) or group >= len(groups_raw):
            raise LpError("partition points outside the tables")
        md.partitions.append(Partition(_name(name), attrs, group, extents[first:first + count]))
    md.groups = [Group(_name(n), f, m) for n, f, m in groups_raw]
    md.block_devices = [BlockDevice(s, a, ao, sz, _name(n), f) for s, a, ao, sz, n, f in bdev_raw]
    if not md.block_devices:
        raise LpError("no block devices in metadata")
    return md


def read_super(f: BinaryIO, slot: int = 0) -> Metadata:
    """Metadata of `slot` from a full super image (primary copy, backup copy as fallback)."""
    f.seek(RESERVED)
    geo = f.read(GEOMETRY_SIZE * 2)
    try:
        max_size, slots, block = _parse_geometry(geo[:GEOMETRY_SIZE])
    except LpError:
        max_size, slots, block = _parse_geometry(geo[GEOMETRY_SIZE:])
    if not 0 <= slot < slots:
        raise LpError(f"metadata slot {slot} doesn't exist")
    base = RESERVED + 2 * GEOMETRY_SIZE
    errors = []
    for offset in (base + slot * max_size, base + (slots + slot) * max_size):
        f.seek(offset)
        try:
            return _parse_metadata(f.read(max_size), max_size, slots, block)
        except LpError as e:
            errors.append(str(e))
    raise LpError(f"no readable metadata for slot {slot}: {'; '.join(errors)}")


def read_image(blob: bytes) -> Metadata:
    """A metadata-only image (geometry + one metadata copy), e.g. super_empty.img."""
    max_size, slots, block = _parse_geometry(blob[:GEOMETRY_SIZE])
    return _parse_metadata(blob[GEOMETRY_SIZE:], max_size, slots, block)


def copy_partition(f: BinaryIO, part: Partition, out: BinaryIO, chunk: int = 1 << 20) -> str:
    """Write a logical partition's bytes from a super image to `out`; returns sha256."""
    h = hashlib.sha256()
    for e in part.extents:
        remaining = e.num_sectors * SECTOR
        if e.target_type == EXTENT_ZERO:
            while remaining:
                data = b"\0" * min(chunk, remaining)
                out.write(data)
                h.update(data)
                remaining -= len(data)
            continue
        if e.target_type != EXTENT_LINEAR or e.target_source != 0:
            raise LpError(f"{part.name}: only extents on the super device are supported")
        f.seek(e.target_data * SECTOR)
        while remaining:
            data = f.read(min(chunk, remaining))
            if not data:
                raise LpError(f"{part.name}: super image is shorter than its metadata says")
            out.write(data)
            h.update(data)
            remaining -= len(data)
    return h.hexdigest()


# --------------------------------------------------------------------- write


def serialize_metadata(md: Metadata) -> bytes:
    extents: list[Extent] = []
    parts = b""
    for p in md.partitions:
        parts += _PART.pack(p.name.encode()[:36], p.attributes, len(extents), len(p.extents), p.group)
        extents += p.extents
    exts = b"".join(_EXT.pack(e.num_sectors, e.target_type, e.target_data, e.target_source) for e in extents)
    groups = b"".join(_GROUP.pack(g.name.encode()[:36], g.flags, g.maximum_size) for g in md.groups)
    bdevs = b"".join(_BDEV.pack(b.first_logical_sector, b.alignment, b.alignment_offset, b.size,
                                b.name.encode()[:36], b.flags) for b in md.block_devices)
    tables = parts + exts + groups + bdevs
    descs = [0, len(md.partitions), _PART.size,
             len(parts), len(extents), _EXT.size,
             len(parts) + len(exts), len(md.groups), _GROUP.size,
             len(parts) + len(exts) + len(groups), len(md.block_devices), _BDEV.size]
    header_size = 256 if md.minor >= 2 else 128

    def head(checksum: bytes) -> bytes:
        h = _HEAD.pack(HEADER_MAGIC, md.major, md.minor, header_size, checksum,
                       len(tables), hashlib.sha256(tables).digest(), *descs)
        if header_size == 256:
            h += struct.pack("<I", md.flags) + b"\0" * 124
        return h

    header = head(hashlib.sha256(head(b"\0" * 32)).digest())
    blob = header + tables
    if len(blob) > md.metadata_max_size:
        raise LpError("metadata is larger than metadata_max_size")
    return blob


def serialize_geometry(md: Metadata) -> bytes:
    zeroed = _GEOM.pack(GEOMETRY_MAGIC, _GEOM.size, b"\0" * 32, md.metadata_max_size, md.slot_count, md.block_size)
    geo = _GEOM.pack(GEOMETRY_MAGIC, _GEOM.size, hashlib.sha256(zeroed).digest(),
                     md.metadata_max_size, md.slot_count, md.block_size)
    return geo.ljust(GEOMETRY_SIZE, b"\0")


def write_image(md: Metadata) -> bytes:
    """Metadata-only image in lpmake's format (geometry + one metadata copy)."""
    return serialize_geometry(md) + serialize_metadata(md)


def write_super_head(md: Metadata) -> bytes:
    """Reserved bytes + both geometries + every metadata copy (start of a super image)."""
    meta = serialize_metadata(md).ljust(md.metadata_max_size, b"\0")
    geo = serialize_geometry(md)
    return b"\0" * RESERVED + geo + geo + meta * (2 * md.slot_count)


def strip_slot(name: str) -> tuple[str, str]:
    return (name[:-2], name[-2:]) if name.endswith(("_a", "_b")) else (name, "")


def empty_layout(md: Metadata, slot_suffix: str) -> Metadata:
    """The layout a recovery needs to rebuild `slot_suffix`'s partitions: both
    slots' groups and partitions, no extents, only the read-only attribute."""
    groups = [Group("default", 0, 0)]
    index = {}
    for g in md.groups:
        base, sfx = strip_slot(g.name)
        if g.name == "default" or not sfx or base in index:
            continue
        for s in ("_a", "_b"):
            index.setdefault(base, {})[s] = len(groups)
            groups.append(Group(base + s, 0, g.maximum_size))
    parts = []
    for p in md.partitions:
        base, sfx = strip_slot(p.name)
        if sfx != slot_suffix or not p.size:
            continue
        gbase, _ = strip_slot(md.groups[p.group].name)
        if gbase not in index:
            raise LpError(f"{p.name} is in a group without a slot suffix")
        for s in ("_a", "_b"):
            parts.append(Partition(base + s, p.attributes & ATTR_READONLY, index[gbase][s]))
    if not parts:
        raise LpError(f"no partitions for slot {slot_suffix} in super")
    return Metadata(md.metadata_max_size, md.slot_count, md.block_size, md.major, md.minor, md.flags,
                    parts, groups, list(md.block_devices))


def read_super_file(path: Path, slot: int = 0) -> Metadata:
    with open(path, "rb") as f:
        return read_super(f, slot)
