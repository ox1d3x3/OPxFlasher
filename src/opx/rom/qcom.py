"""EXPERIMENTAL: reading Qualcomm anti-rollback (ARB) versions.

Qualcomm boot firmware (xbl, xbl_config, abl, tz ...) is a signed ELF. Its
hash segment starts with an MBN header; on MBN v6/v7 the OEM (and QTI)
metadata blocks carry an ``anti_rollback_version`` field, which is what the
hardware fuses are compared against.

The field layout here follows public reverse-engineering notes and has NOT
yet been validated against real OnePlus 13 firmware. OPxFlasher therefore
treats these values as informational only: a profile must set
``qcom_arb_validated: true`` before the plan will trust them, and until then
a missing or unvalidated value blocks flashing (fail closed).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HASH_SEGMENT_TYPE = 2  # (p_flags >> 24) & 0x7


class QcomError(ValueError):
    pass


@dataclass
class QcomArb:
    mbn_version: int
    software_id: int | None
    oem_arb: int | None
    qti_arb: int | None
    note: str = ""

    @property
    def value(self) -> int | None:
        """The ARB version we compare (OEM metadata first, then QTI)."""
        return self.oem_arb if self.oem_arb is not None else self.qti_arb


def _program_headers(data: bytes) -> list[tuple[int, int, int]]:
    """Return (flags, offset, filesz) for each ELF program header."""
    if data[:4] != b"\x7fELF":
        raise QcomError("not an ELF image")
    cls, endian = data[4], data[5]
    if endian != 1:
        raise QcomError("big-endian ELF not supported")
    out = []
    if cls == 1:  # ELF32
        phoff, = struct.unpack_from("<I", data, 0x1C)
        phentsize, phnum = struct.unpack_from("<HH", data, 0x2A)
        for i in range(phnum):
            base = phoff + i * phentsize
            _t, off, _va, _pa, filesz, _mem, flags, _al = struct.unpack_from("<8I", data, base)
            out.append((flags, off, filesz))
    elif cls == 2:  # ELF64
        phoff, = struct.unpack_from("<Q", data, 0x20)
        phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
        for i in range(phnum):
            base = phoff + i * phentsize
            _t, flags, off, _va, _pa, filesz, _mem, _al = struct.unpack_from("<IIQQQQQQ", data, base)
            out.append((flags, off, filesz))
    else:
        raise QcomError("unknown ELF class")
    return out


def read_arb(data: bytes) -> QcomArb:
    segs = [(off, size) for flags, off, size in _program_headers(data) if (flags >> 24) & 0x7 == HASH_SEGMENT_TYPE]
    if not segs:
        raise QcomError("no Qualcomm hash segment found")
    off, size = segs[0]
    seg = data[off : off + size]
    if len(seg) < 36:
        raise QcomError("hash segment truncated")

    version = struct.unpack_from("<I", seg, 0)[0]
    if version not in (6, 7):
        legacy = struct.unpack_from("<I", seg, 4)[0]
        return QcomArb(
            mbn_version=legacy if legacy in (3, 5) else version,
            software_id=None,
            oem_arb=None,
            qti_arb=None,
            note="Legacy MBN header: ARB is kept in the certificate and is not read yet.",
        )

    (_, common_size, qti_size, oem_size, *_rest) = struct.unpack_from("<9I", seg, 0)
    pos = 36
    software_id = None
    if common_size >= 12:
        software_id = struct.unpack_from("<I", seg, pos + 8)[0]
    pos += common_size

    def metadata_arb(start: int, length: int) -> int | None:
        if length >= 12 and start + 12 <= len(seg):
            return struct.unpack_from("<I", seg, start + 8)[0]
        return None

    qti_arb = metadata_arb(pos, qti_size)
    pos += qti_size
    oem_arb = metadata_arb(pos, oem_size)
    return QcomArb(mbn_version=version, software_id=software_id, oem_arb=oem_arb, qti_arb=qti_arb)
