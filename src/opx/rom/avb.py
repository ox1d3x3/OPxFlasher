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
    from_footer: bool = False

    @property
    def verification_disabled(self) -> bool:
        return bool(self.flags & FLAG_VERIFICATION_DISABLED)

    @property
    def hashtree_disabled(self) -> bool:
        return bool(self.flags & FLAG_HASHTREE_DISABLED)


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
        elif tag == TAG_CHAIN and n >= 76:
            location, name_len = struct.unpack(">II", body[:8])
            vb.chains.append((body[76 : 76 + name_len].decode("utf-8", "replace"), location))
