"""Reading Android Verified Boot (AVB 2.0) metadata.

Parses a vbmeta image (``AVB0``) or an image with an AVB footer (``AVBf``)
and returns its rollback index, rollback location, flags and descriptors.
Layout follows libavb's avb_vbmeta_image.h / avb_footer.h (big-endian).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

HEADER_FMT = ">4sIIQQI" + "Q" * 10 + "QII48s80s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 256
FOOTER_FMT = ">4sIIQQQ28s"
FOOTER_SIZE = struct.calcsize(FOOTER_FMT)
assert FOOTER_SIZE == 64

TAG_PROPERTY, TAG_HASHTREE, TAG_HASH, TAG_CMDLINE, TAG_CHAIN = 0, 1, 2, 3, 4

FLAG_HASHTREE_DISABLED = 1
FLAG_VERIFICATION_DISABLED = 2


class AvbError(ValueError):
    pass


@dataclass
class HashDescriptor:
    """What an image must hash to: digest = hash(salt + image[:image_size])."""

    partition: str
    image_size: int
    algorithm: str
    salt: bytes
    digest: bytes


@dataclass
class HashtreeDescriptor:
    """dm-verity: the root of a hash tree over image[:image_size], block by block."""

    partition: str
    image_size: int
    data_block_size: int
    hash_block_size: int
    algorithm: str
    salt: bytes
    root_digest: bytes


def hashtree_root(read, desc: HashtreeDescriptor, tick=None) -> bytes:
    """Compute the dm-verity root digest of a stream. `read(n)` returns the next n bytes of
    the image; exactly desc.image_size bytes are consumed. Raises AvbError if it's short."""
    import hashlib

    algo = {"sha256": hashlib.sha256, "sha1": hashlib.sha1, "sha512": hashlib.sha512}.get(desc.algorithm)
    bs, hbs = desc.data_block_size, desc.hash_block_size
    if algo is None or bs <= 0 or hbs <= 0 or desc.image_size % bs:
        raise AvbError(f"unsupported hash tree ({desc.algorithm or '?'}, block {bs})")
    size = algo().digest_size
    pad = b"\0" * ((1 << (size - 1).bit_length()) - size)  # digests are padded to a power of two (sha1)
    level = bytearray()
    done = 0
    while done < desc.image_size:
        want = min(bs * 256, desc.image_size - done)
        chunk = read(want)
        if len(chunk) != want:
            raise AvbError("image is shorter than the data its hash tree covers")
        if desc.image_size == bs:  # one block: its own hash is the root (as avbtool does)
            return algo(desc.salt + chunk).digest()
        for i in range(0, want, bs):
            level += algo(desc.salt + chunk[i : i + bs]).digest() + pad
        done += want
        if tick:
            tick(done, desc.image_size)
    while True:  # same construction as avbtool: pad each level to whole blocks, hash upwards
        if len(level) % hbs:
            level += b"\0" * (hbs - len(level) % hbs)
        if len(level) <= hbs:
            return algo(desc.salt + bytes(level)).digest()
        nxt = bytearray()
        for i in range(0, len(level), hbs):
            nxt += algo(desc.salt + level[i : i + hbs]).digest() + pad
        level = nxt


@dataclass
class Vbmeta:
    rollback_index: int
    rollback_index_location: int
    flags: int
    algorithm: int
    release: str
    libavb_version: str
    properties: dict[str, str] = field(default_factory=dict)
    hash_partitions: list[str] = field(default_factory=list)
    hashtree_partitions: list[str] = field(default_factory=list)
    chains: list[tuple[str, int]] = field(default_factory=list)  # (partition, rollback location)
    hashes: list[HashDescriptor] = field(default_factory=list)
    hashtree_sizes: dict[str, int] = field(default_factory=dict)  # partition -> protected data size
    hashtrees: list[HashtreeDescriptor] = field(default_factory=list)
    from_footer: bool = False

    @property
    def verification_disabled(self) -> bool:
        return bool(self.flags & FLAG_VERIFICATION_DISABLED)

    @property
    def hashtree_disabled(self) -> bool:
        return bool(self.flags & FLAG_HASHTREE_DISABLED)


def parse_file_footer(path) -> Vbmeta | None:
    """The vbmeta appended to an image file (AVB footer), read without loading the image."""
    import os

    size = os.path.getsize(path)
    if size < FOOTER_SIZE:
        return None
    with open(path, "rb") as fh:
        fh.seek(size - FOOTER_SIZE)
        tail = fh.read(FOOTER_SIZE)
        if tail[:4] != b"AVBf":
            return None
        _, _, _, _orig, vb_off, vb_size, _ = struct.unpack(FOOTER_FMT, tail)
        if vb_off + vb_size > size or vb_size > (8 << 20):
            raise AvbError("AVB footer points outside the image")
        fh.seek(vb_off)
        return _parse_vbmeta(fh.read(vb_size), from_footer=True)


def has_avb(data: bytes) -> bool:
    return data[:4] == b"AVB0" or (len(data) >= FOOTER_SIZE and data[-FOOTER_SIZE:][:4] == b"AVBf")


def parse(data: bytes) -> Vbmeta:
    if data[:4] == b"AVB0":
        return _parse_vbmeta(data, from_footer=False)
    if len(data) >= FOOTER_SIZE and data[-FOOTER_SIZE:][:4] == b"AVBf":
        _, _, _, _orig, vb_off, vb_size, _ = struct.unpack(FOOTER_FMT, data[-FOOTER_SIZE:])
        if vb_off + vb_size > len(data):
            raise AvbError("AVB footer points outside the image")
        return _parse_vbmeta(data[vb_off : vb_off + vb_size], from_footer=True)
    raise AvbError("no AVB metadata (neither vbmeta header nor footer)")


def _parse_vbmeta(data: bytes, from_footer: bool) -> Vbmeta:
    if len(data) < HEADER_SIZE:
        raise AvbError("vbmeta header truncated")
    f = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
    (
        magic, ver_major, ver_minor, auth_size, aux_size, algorithm,
        _hash_off, _hash_size, _sig_off, _sig_size, _pk_off, _pk_size, _pkmd_off, _pkmd_size,
        desc_off, desc_size, rollback_index, flags, rollback_location, release, _reserved,
    ) = f
    if magic != b"AVB0":
        raise AvbError("not a vbmeta image (bad magic)")
    aux_start = HEADER_SIZE + auth_size
    if aux_start + aux_size > len(data):
        raise AvbError("vbmeta blocks extend past the end of the image")
    vb = Vbmeta(
        rollback_index=rollback_index,
        rollback_index_location=rollback_location,
        flags=flags,
        algorithm=algorithm,
        release=release.split(b"\0", 1)[0].decode("utf-8", "replace"),
        libavb_version=f"{ver_major}.{ver_minor}",
        from_footer=from_footer,
    )
    start = aux_start + desc_off
    _parse_descriptors(data[start : start + desc_size], vb)
    return vb


def _parse_descriptors(blob: bytes, vb: Vbmeta) -> None:
    pos = 0
    while pos + 16 <= len(blob):
        tag, n = struct.unpack(">QQ", blob[pos : pos + 16])
        body = blob[pos + 16 : pos + 16 + n]
        pos += 16 + n
        if len(body) < n:
            raise AvbError("descriptor truncated")
        if tag == TAG_PROPERTY and n >= 16:
            klen, vlen = struct.unpack(">QQ", body[:16])
            key = body[16 : 16 + klen].decode("utf-8", "replace")
            value = body[16 + klen + 1 : 16 + klen + 1 + vlen].decode("utf-8", "replace")
            vb.properties[key] = value
        elif tag == TAG_HASH and n >= 116:
            image_size = struct.unpack(">Q", body[0:8])[0]
            algorithm = body[8:40].split(b"\0", 1)[0].decode("ascii", "replace")
            name_len, salt_len, digest_len = struct.unpack(">III", body[40:52])
            name = body[116 : 116 + name_len].decode("utf-8", "replace")
            salt = body[116 + name_len : 116 + name_len + salt_len]
            digest = body[116 + name_len + salt_len : 116 + name_len + salt_len + digest_len]
            vb.hash_partitions.append(name)
            vb.hashes.append(HashDescriptor(name, image_size, algorithm, salt, digest))
        elif tag == TAG_HASHTREE and n >= 164:
            image_size = struct.unpack(">Q", body[4:12])[0]
            name_len = struct.unpack(">I", body[88:92])[0]
            name = body[164 : 164 + name_len].decode("utf-8", "replace")
            vb.hashtree_partitions.append(name)
            vb.hashtree_sizes[name] = image_size
            dbs, hbs = struct.unpack(">II", body[28:36])
            algorithm = body[56:88].split(b"\0", 1)[0].decode("ascii", "replace")
            salt_len, root_len = struct.unpack(">II", body[92:100])
            salt = body[164 + name_len : 164 + name_len + salt_len]
            root = body[164 + name_len + salt_len : 164 + name_len + salt_len + root_len]
            vb.hashtrees.append(HashtreeDescriptor(name, image_size, dbs, hbs, algorithm, salt, root))
        elif tag == TAG_CHAIN and n >= 76:
            location, name_len = struct.unpack(">II", body[:8])
            vb.chains.append((body[76 : 76 + name_len].decode("utf-8", "replace"), location))
