"""Builders for synthetic firmware used in tests.

These produce structurally valid payload.bin / OTA zips / vbmeta images /
Qualcomm-style signed ELFs so the readers can be tested end to end. They only
prove OPxFlasher is self-consistent; real-firmware validation is separate.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import struct
import zipfile
from pathlib import Path

BLOCK = 4096


# --- protobuf encoding ------------------------------------------------------


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def f_int(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


def f_bytes(num: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return varint((num << 3) | 2) + varint(len(value)) + value


# --- payload.bin ------------------------------------------------------------


def pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % BLOCK)


def build_payload(
    partitions: dict[str, bytes],
    op_types: dict[str, int] | None = None,
    groups: dict[str, list[str]] | None = None,
    incremental: set[str] | None = None,
    minor_version: int = 0,
    security_patch: str = "2026-09-01",
    corrupt: str | None = None,
    old_info: dict[str, bytes] | None = None,
) -> bytes:
    """`old_info`: raw old_partition_info message per partition (b"" = present but empty)."""
    old_info = old_info or {}
    op_types = op_types or {}
    incremental = incremental or set()
    blob = bytearray()
    parts_pb = b""
    for name, data in partitions.items():
        data = pad(data)
        op_type = op_types.get(name, 0)
        if op_type == 1:
            stored = bz2.compress(data)
        elif op_type == 8:
            stored = lzma.compress(data, format=lzma.FORMAT_XZ)
        else:
            stored = data
        offset = len(blob)
        blob += stored
        if corrupt == name:
            blob[offset] ^= 0xFF
        extent = f_int(1, 0) + f_int(2, len(data) // BLOCK)
        op = (
            f_int(1, 4 if name in incremental else op_type)
            + f_int(2, offset)
            + f_int(3, len(stored))
            + f_bytes(6, extent)
            + f_bytes(8, hashlib.sha256(stored).digest() if corrupt != name else hashlib.sha256(bytes(stored)).digest())
        )
        info = f_int(1, len(data)) + f_bytes(2, hashlib.sha256(data).digest())
        part = f_bytes(1, name) + f_bytes(7, info) + f_bytes(8, op)
        if name in incremental:
            part += f_bytes(6, f_int(1, len(data)))
        elif name in old_info:
            part += f_bytes(6, old_info[name])
        parts_pb += f_bytes(13, part)

    manifest = f_int(3, BLOCK) + f_int(12, minor_version) + parts_pb + f_bytes(18, security_patch)
    if groups:
        dyn = b""
        for gname, names in groups.items():
            size = sum(len(pad(partitions[n])) for n in names) * 2
            g = f_bytes(1, gname) + f_int(2, size) + b"".join(f_bytes(3, n) for n in names)
            dyn += f_bytes(1, g)
        manifest += f_bytes(15, dyn)
    header = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0)
    return header + manifest + bytes(blob)


def build_ota_zip(path: Path, payload: bytes, metadata: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/com/android/metadata", "".join(f"{k}={v}\n" for k, v in metadata.items()))
        zf.writestr(zipfile.ZipInfo("payload_properties.txt"), "FILE_HASH=x\n")
        info = zipfile.ZipInfo("payload.bin")
        info.compress_type = zipfile.ZIP_STORED
        info.extra = b"\x00\x00\x04\x00abcd"  # make sure we honour local extra fields
        zf.writestr(info, payload)
    return path


def ota_metadata(codename="testdevice", product="PJZ110", android="16", patch="2026-09-01", incremental=False):
    meta = {
        "ota-type": "AB",
        "pre-device": codename,
        "post-build": f"OnePlus/{product}/{codename}:{android}/BP2A.250605.015/U.1:user/release-keys",
        "post-build-incremental": "U.1",
        "post-security-patch-level": patch,
        "post-timestamp": "1790000000",
    }
    if incremental:
        meta["pre-build"] = f"OnePlus/{product}/{codename}:{android}/OLD/U.0:user/release-keys"
    return meta


# --- AVB ---------------------------------------------------------------------


def _desc(tag: int, body: bytes) -> bytes:
    body += b"\0" * (-len(body) % 8)
    return struct.pack(">QQ", tag, len(body)) + body


def verity_root(data: bytes, salt: bytes, bs: int = 4096) -> bytes:
    """dm-verity root the way avbtool builds it (sha256)."""
    h = lambda b: hashlib.sha256(salt + b).digest()
    if len(data) == bs:
        return h(data)
    level = b"".join(h(data[i : i + bs]) for i in range(0, len(data), bs))
    while True:
        level += b"\0" * (-len(level) % bs)
        if len(level) == bs:
            return h(level)
        level = b"".join(h(level[i : i + bs]) for i in range(0, len(level), bs))


def build_vbmeta(rollback_index: int, location: int = 0, chains=(), props=None, hashes=(), flags: int = 0,
                 hashtrees=()) -> bytes:
    descs = b""
    for name, data in hashtrees:  # (partition, image bytes: whole 4096-byte blocks)
        n, salt = name.encode(), b"verity-salt"
        root = verity_root(data, salt)
        body = struct.pack(">IQQQIIIQQ32sIIII60s", 1, len(data), len(data), 0, 4096, 4096, 0, 0, 0, b"sha256",
                           len(n), len(salt), len(root), 0, b"") + n + salt + root
        descs += _desc(1, body)
    for key, value in (props or {}).items():
        k, v = key.encode(), value.encode()
        descs += _desc(0, struct.pack(">QQ", len(k), len(v)) + k + b"\0" + v + b"\0")
    for entry in hashes:  # a name (dummy digest) or (name, image bytes) for a real one
        name, data = (entry, None) if isinstance(entry, str) else entry
        n, salt = name.encode(), b"salt1234"
        if data is None:
            body = struct.pack(">Q32sIIII60s", 4096, b"sha256", len(n), 0, 32, 0, b"") + n + b"\0" * 32
        else:
            digest = hashlib.sha256(salt + data).digest()
            body = struct.pack(">Q32sIIII60s", len(data), b"sha256", len(n), len(salt), 32, 0, b"") + n + salt + digest
        descs += _desc(2, body)
    for name, loc in chains:
        n = name.encode()
        body = struct.pack(">IIII60s", loc, len(n), 8, 0, b"") + n + b"K" * 8
        descs += _desc(4, body)
    aux = descs + b"\0" * (-len(descs) % 64)
    header = struct.pack(
        ">4sIIQQI" + "Q" * 10 + "QII48s80s",
        b"AVB0", 1, 0, 0, len(aux), 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, len(descs),
        rollback_index, flags, location, b"avbtool 1.3.0", b"",
    )
    return header + aux


def with_footer(image: bytes, vbmeta: bytes) -> bytes:
    body = image + vbmeta
    footer = struct.pack(">4sIIQQQ28s", b"AVBf", 1, 0, len(image), len(image), len(vbmeta), b"")
    return body + b"\0" * 64 + footer


# --- Qualcomm signed ELF ----------------------------------------------------


def build_qcom_elf(arb: int, mbn_version: int = 6, software_id: int = 0x1C) -> bytes:
    if mbn_version in (6, 7):
        common = struct.pack("<6I", 0, 0, software_id, 0, 3, 0)
        oem = struct.pack("<4I", 0, 0, arb, 0) + b"\0" * (120 - 16)
        seg = struct.pack("<9I", mbn_version, len(common), 0, len(oem), 0, 0, 0, 0, 0) + common + oem
    else:
        seg = struct.pack("<10I", 0, 3, 0, 0, 0, 0, 0, 0, 0, 0)
    ehdr_size, phent = 64, 56
    phoff = ehdr_size
    seg_off = phoff + 2 * phent
    load = b"\x90" * 64
    load_off = seg_off + len(seg)
    ehdr = (
        b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
        + struct.pack("<HHIQQQIHHHHHH", 2, 0xB7, 1, 0, phoff, 0, 0, ehdr_size, phent, 2, 0, 0, 0)
    )
    ph_hash = struct.pack("<IIQQQQQQ", 0, 2 << 24, seg_off, 0, 0, len(seg), len(seg), 0)
    ph_load = struct.pack("<IIQQQQQQ", 1, 5, load_off, 0, 0, len(load), len(load), 0)
    return ehdr + ph_hash + ph_load + seg + load


# --- boot images ----------------------------------------------------------------


def build_boot_image(ramdisk: bytes = b"ramdisk", header_version: int = 4, size: int = 8192) -> bytes:
    """A minimal Android boot/init_boot image (v3/v4 header layout)."""
    header = b"ANDROID!" + struct.pack("<IIII4II", 0, len(ramdisk), 0, 4096, 0, 0, 0, 0, header_version)
    body = header + b"\0" * (4096 - len(header)) + ramdisk
    return body + b"\0" * (size - len(body))
