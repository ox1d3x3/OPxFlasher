"""A minimal protobuf wire-format decoder.

Enough to read Android's update_engine ``DeltaArchiveManifest`` without
pulling in protobuf or generated code. Messages are decoded into
``{field_number: [values]}``; nested messages stay as raw bytes until the
caller decodes them.
"""

from __future__ import annotations

from collections import defaultdict

VARINT, I64, LEN, I32 = 0, 1, 2, 5


class ProtoError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ProtoError("varint too long")


def decode(buf: bytes) -> dict[int, list]:
    fields: dict[int, list] = defaultdict(list)
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == I64:
            value, pos = int.from_bytes(buf[pos : pos + 8], "little"), pos + 8
        elif wire == LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > len(buf):
                raise ProtoError("truncated field")
            value, pos = bytes(buf[pos : pos + length]), pos + length
        elif wire == I32:
            value, pos = int.from_bytes(buf[pos : pos + 4], "little"), pos + 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        fields[number].append(value)
    return fields


def first(fields: dict[int, list], number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


def text(fields: dict[int, list], number: int, default: str = "") -> str:
    value = first(fields, number)
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else default


def unpacked_ints(fields: dict[int, list], number: int) -> list[int]:
    """Repeated integers, accepting both packed and unpacked encodings."""
    out: list[int] = []
    for value in fields.get(number, []):
        if isinstance(value, bytes):
            pos = 0
            while pos < len(value):
                v, pos = read_varint(value, pos)
                out.append(v)
        else:
            out.append(value)
    return out
