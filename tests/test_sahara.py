"""9008 identify against a simulated Sahara boot ROM."""

import struct

import pytest

from opx import cli, sahara as sh

SERIAL, HWID, PK = 0x1A2B3C4D, 0x0022E0E100510000, bytes(range(32))


class FakeBootRom:
    """Speaks just enough Sahara; records everything the host sends."""

    def __init__(self, command_mode=True, hello_now=True, pk_twice=True, version=2, supported=(1, 2, 3, 7)):
        self.out = bytearray()
        self.sent: list[tuple[int, tuple]] = []
        self.command_mode, self.pk_twice, self.version, self.supported = command_mode, pk_twice, version, supported
        self.pending = None
        if hello_now:
            self._hello()

    def _hello(self):
        self.out += struct.pack("<12I", sh.HELLO, 0x30, self.version, 1, 0x400, sh.MODE_IMAGE_TX_PENDING, *[0] * 6)

    def read(self, n):
        data, self.out[:] = bytes(self.out[:n]), self.out[n:]
        return data

    def write(self, data):
        cmd, length = struct.unpack_from("<II", data)
        words = struct.unpack_from("<" + "I" * ((length - 8) // 4), data, 8)
        self.sent.append((cmd, words))
        if cmd == sh.RESET_STATE_MACHINE:
            self._hello()
        elif cmd == sh.HELLO_RESP:
            if self.command_mode and words[3] == sh.MODE_COMMAND:
                self.out += struct.pack("<II", sh.CMD_READY, 8)
            else:
                self.out += struct.pack("<IIII", sh.END_IMAGE_TX, 0x10, 0, 0x0B)
        elif cmd == sh.EXECUTE:
            what = words[0]
            if what not in self.supported:
                self.out += struct.pack("<IIII", sh.END_IMAGE_TX, 0x10, 0, 0x0C)
                return
            self.pending = {sh.EXEC_SERIAL: struct.pack("<I", SERIAL), sh.EXEC_HWID: struct.pack("<Q", HWID),
                            sh.EXEC_PK_HASH: PK * (2 if self.pk_twice else 1),
                            sh.EXEC_SBL_VERSION: struct.pack("<I", 7)}[what]
            self.out += struct.pack("<IIII", sh.EXECUTE_RESP, 0x10, what, len(self.pending))
        elif cmd == sh.EXECUTE_DATA:
            self.out += self.pending
        return len(data)


def test_identify_reads_details_and_writes_nothing_else():
    rom = FakeBootRom()
    info = sh.identify(rom, "COM5")
    assert info.sahara_version == 2 and info.serial == "0x1a2b3c4d"
    assert info.hwid == "0x0022e0e100510000" and info.msm_id == "0x0022e0e1" and info.oem_id == "0x0051"
    assert info.pk_hash == PK.hex() and info.sbl_version == "0x00000007"
    # only greetings, questions and the switch back: never an image / loader
    assert {c for c, _ in rom.sent} <= {sh.HELLO_RESP, sh.EXECUTE, sh.EXECUTE_DATA, sh.SWITCH_MODE}
    assert rom.sent[-1] == (sh.SWITCH_MODE, (sh.MODE_IMAGE_TX_PENDING,))
    assert any("Chip serial: 0x1a2b3c4d" == line for line in info.lines())


def test_late_open_asks_for_a_new_hello():
    rom = FakeBootRom(hello_now=False)
    info = sh.identify(rom)
    assert rom.sent[0][0] == sh.RESET_STATE_MACHINE and info.serial


def test_no_command_mode():
    info = sh.identify(FakeBootRom(command_mode=False, version=3))
    assert info.sahara_version == 3 and not info.serial
    assert "doesn't answer identification" in info.notes[0]


def test_some_questions_unsupported():
    info = sh.identify(FakeBootRom(supported=(1,), pk_twice=False))
    assert info.serial and not info.hwid and not info.pk_hash


def test_silent_or_garbage_port():
    class Silent:
        def read(self, n):
            return b""

        def write(self, data):
            return len(data)
    with pytest.raises(sh.SaharaError, match="stopped answering"):
        sh.identify(Silent())

    class Junk(FakeBootRom):
        def _hello(self):
            self.out += struct.pack("<II", 0x55, 0xFFFFFF)
    with pytest.raises(sh.SaharaError, match="Unexpected Sahara packet"):
        sh.identify(Junk())


def test_cli_edl_info(monkeypatch, capsys):
    monkeypatch.setattr(sh, "find_ports", lambda: [])
    assert cli.main(["edl-info"]) == 2
    assert "QDLoader" in capsys.readouterr().out
    monkeypatch.setattr(sh, "identify_port", lambda port: sh.identify(FakeBootRom(), port))
    assert cli.main(["edl-info", "--port", "/dev/ttyUSB0"]) == 0
    out = capsys.readouterr().out
    assert "Port: /dev/ttyUSB0" in out and "Chip serial" in out
