"""Identify a phone in Qualcomm 9008 (EDL) mode: read-only.

In 9008 the phone's boot ROM speaks Qualcomm's Sahara protocol over a USB
serial port (Windows: "Qualcomm HS-USB QDLoader 9008 (COMx)"; Linux: the
qcserial driver's /dev/ttyUSBx). Before any programmer is loaded, Sahara's
command mode answers a few questions: chip serial number, MSM hardware ID,
OEM public-key hash and the boot ROM version. That's all this module does.

It never sends a programmer ("firehose loader") and can't write anything.
EDL flashing on OnePlus / OPPO phones needs OnePlus's signed loader and their
authorisation, which only authorised service centres have; the details read
here are what helps them (and you) identify the phone.

Written from the publicly documented Sahara packet layout.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

HELLO, HELLO_RESP, END_IMAGE_TX = 0x01, 0x02, 0x04
CMD_READY, SWITCH_MODE, EXECUTE, EXECUTE_RESP, EXECUTE_DATA = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
RESET_STATE_MACHINE = 0x13
MODE_IMAGE_TX_PENDING, MODE_COMMAND = 0x0, 0x3
EXEC_SERIAL, EXEC_HWID, EXEC_PK_HASH, EXEC_SBL_VERSION = 0x01, 0x02, 0x03, 0x07

QUALCOMM_VID, EDL_PID = 0x05C6, 0x9008
MAX_PACKET = 0x1000


class SaharaError(RuntimeError):
    pass


@dataclass
class EdlInfo:
    port: str
    sahara_version: int = 0
    serial: str = ""
    hwid: str = ""
    msm_id: str = ""
    oem_id: str = ""
    model_id: str = ""
    pk_hash: str = ""
    sbl_version: str = ""
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"Port: {self.port}", f"Sahara protocol version: {self.sahara_version or '?'}"]
        for label, value in (("Chip serial", self.serial), ("Hardware ID", self.hwid), ("MSM ID", self.msm_id),
                             ("OEM ID", self.oem_id), ("Model ID", self.model_id),
                             ("OEM key hash", self.pk_hash), ("Boot ROM version", self.sbl_version)):
            if value:
                out.append(f"{label}: {value}")
        return out + self.notes


# --- packets -----------------------------------------------------------------------


def _read_exact(port, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = port.read(n - len(data))
        if not chunk:
            raise SaharaError("The phone stopped answering.")
        data += chunk
    return data


def read_packet(port) -> tuple[int, bytes]:
    head = _read_exact(port, 8)
    cmd, length = struct.unpack("<II", head)
    if not 8 <= length <= MAX_PACKET:
        raise SaharaError(f"Unexpected Sahara packet (command {cmd:#x}, length {length}).")
    return cmd, head + _read_exact(port, length - 8)


def _send(port, *words: int) -> None:
    port.write(struct.pack("<" + "I" * (len(words) + 1), words[0], 4 * (len(words) + 1), *words[1:]))


# --- identify ------------------------------------------------------------------------


def _hello(port) -> tuple[int, int]:
    try:
        cmd, pkt = read_packet(port)
    except SaharaError:
        # opened after the phone said hello: ask it to start over (Sahara v2+)
        _send(port, RESET_STATE_MACHINE)
        cmd, pkt = read_packet(port)
    if cmd != HELLO or len(pkt) < 24:
        raise SaharaError("The phone didn't greet in Sahara (is it really in 9008 mode?).")
    version, supported = struct.unpack_from("<II", pkt, 8)
    return version, supported


def _execute(port, what: int) -> bytes | None:
    _send(port, EXECUTE, what)
    cmd, pkt = read_packet(port)
    if cmd != EXECUTE_RESP:
        return None  # not supported on this phone
    client, length = struct.unpack_from("<II", pkt, 8)
    if client != what or length > MAX_PACKET:
        raise SaharaError("Mismatched Sahara answer.")
    _send(port, EXECUTE_DATA, what)
    return _read_exact(port, length) if length else b""


def identify(port, name: str = "") -> EdlInfo:
    info = EdlInfo(port=name)
    version, supported = _hello(port)
    info.sahara_version = version
    # answer the greeting asking for command mode (no image transfer)
    port.write(struct.pack("<12I", HELLO_RESP, 0x30, version, supported, 0, MODE_COMMAND, *([0] * 6)))
    cmd, pkt = read_packet(port)
    if cmd != CMD_READY:
        info.notes.append("This phone doesn't answer identification questions (no Sahara command mode). "
                          "It is in 9008 mode.")
        return info
    try:
        data = _execute(port, EXEC_SERIAL)
        if data and len(data) >= 4:
            info.serial = f"0x{struct.unpack_from('<I', data)[0]:08x}"
        data = _execute(port, EXEC_HWID)
        if data and len(data) >= 8:
            hwid = struct.unpack_from("<Q", data)[0]
            info.hwid = f"0x{hwid:016x}"
            info.msm_id = f"0x{hwid >> 32:08x}"
            info.oem_id = f"0x{(hwid >> 16) & 0xFFFF:04x}"
            info.model_id = f"0x{hwid & 0xFFFF:04x}"
        data = _execute(port, EXEC_PK_HASH)
        if data:
            half = len(data) // 2
            if half and data[:half] == data[half:]:
                data = data[:half]  # some boot ROMs send the hash twice
            info.pk_hash = data.hex()
        data = _execute(port, EXEC_SBL_VERSION)
        if data and len(data) >= 4:
            info.sbl_version = f"0x{struct.unpack_from('<I', data)[0]:08x}"
    finally:
        # back to waiting for a loader; OPxFlasher never sends one
        try:
            _send(port, SWITCH_MODE, MODE_IMAGE_TX_PENDING)
        except OSError:
            pass
    if not (info.serial or info.hwid or info.pk_hash):
        info.notes.append("The phone answered but shared no details (newer boot ROMs hide some).")
    info.notes.append("Nothing was written. To leave 9008, hold Power + Volume Up + Volume Down for about "
                      "15 seconds.")
    return info


# --- finding the port ----------------------------------------------------------------


def find_ports() -> list[str]:
    """Serial ports of phones in 9008 mode (needs the QDLoader driver on Windows)."""
    from serial.tools import list_ports

    return [p.device for p in list_ports.comports() if p.vid == QUALCOMM_VID and p.pid == EDL_PID]


def identify_port(device: str, timeout: float = 3.0) -> EdlInfo:
    import serial

    try:
        with serial.Serial(device, 115200, timeout=timeout) as port:
            return identify(port, device)
    except PermissionError as e:
        raise SaharaError(f"Not allowed to open {device}. On Linux, add yourself to the 'dialout' group "
                          "(sudo usermod -aG dialout $USER), log out and in, then try again.") from e
    except serial.SerialException as e:
        raise SaharaError(f"Couldn't open {device}: {e}") from e


def identify_first() -> EdlInfo:
    ports = find_ports()
    if not ports:
        raise SaharaError("No phone in 9008 (EDL) mode was found as a serial port. On Windows the "
                          "'Qualcomm HS-USB QDLoader 9008' driver is needed for this (with a WinUSB / Zadig "
                          "driver it doesn't show up as a port).")
    return identify_port(ports[0])
