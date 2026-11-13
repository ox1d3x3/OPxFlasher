"""OnePlus 13 / 13T / 15 hardware anti-rollback fuse (ColorOS 16.0.3.501+)."""

from pathlib import Path

import pytest
import romfactory as rf
from test_flash import ROM_PARTS, phone  # noqa: F401  (fixture)

from opx import plan as pn
from opx import profile as profiles
from opx.rom.ota import RomInfo, open_rom
from opx.session import open_session

OP13 = profiles.load("oneplus-13")


def dev(build="PJZ110_16.0.3.501(CN01)", date="1780000000", profile=OP13):
    return pn.DeviceState("live", "test", {"build": build}, {"ro.build.date.utc": date}, profile)


def rom(display="", stamp=""):
    meta = {k: v for k, v in (("post-display-id", display), ("post-timestamp", stamp)) if v}
    return RomInfo(Path("x"), "folder", meta)


def test_os_version():
    assert pn.os_version("PJZ110_16.0.3.501(CN01)") == (16, 0, 3, 501)
    assert pn.os_version("CPH2653_16.0.5.700(EX01)") == (16, 0, 5, 700)
    assert pn.os_version("BP2A.250605") is None
    assert OP13.rollback_fuse == "16.0.3.501"


@pytest.mark.parametrize("r,status,words", [
    (rom("PJZ110_16.0.3.300(CN01)"), pn.BLOCK, "blows an anti-rollback fuse"),
    (rom("PJZ110_16.0.3.501(CN01)"), pn.PASS, "Not older"),
    (rom("PJZ110_16.0.5.100(CN01)"), pn.PASS, "Not older"),
    (rom(stamp="1770000000"), pn.BLOCK, "by build date"),
    (rom(stamp="1790000000"), pn.PASS, "by build date"),
    (rom(), pn.BLOCK, "can't tell this ROM's age"),
])
def test_fuse_active(r, status, words):
    c = pn.rollback_fuse_check(r, dev())
    assert c.status == status and words in c.detail


def test_unknown_installed_build_fails_closed():
    c = pn.rollback_fuse_check(rom(), dev(build="", date=""))
    assert c.status == pn.BLOCK
    c = pn.rollback_fuse_check(rom(stamp="1770000000"), dev(build="weird", date="1780000000"))
    assert c.status == pn.BLOCK


def test_fuse_not_active_yet():
    c = pn.rollback_fuse_check(rom("PJZ110_16.0.2.100(CN01)"), dev(build="PJZ110_16.0.2.400(CN01)"))
    assert c.status == pn.INFO
    c = pn.rollback_fuse_check(rom("PJZ110_16.0.3.501(CN01)"), dev(build="PJZ110_16.0.2.400(CN01)"))
    assert c.status == pn.WARN and "turns on" in c.detail


def test_other_phones_unaffected():
    assert pn.rollback_fuse_check(rom(), dev(profile=profiles.load("oplus-generic"))) is None


def test_profile_validation():
    assert profiles.validate({"id": "x", "name": "X", "inherits": "oplus-generic",
                              "match": {"ro.product.model": ["A1"]}, "rollback_fuse": "16.0"})


def test_report_blocks_older_ota(adb, phone, tmp_path):
    meta = rf.ota_metadata()
    meta["post-timestamp"] = "1770000000"  # built before the installed one
    r = open_rom(rf.build_ota_zip(tmp_path / "old.zip", rf.build_payload(
        ROM_PARTS, groups={"main": ["system", "my_region"]}), meta))
    report = pn.build_report(r, pn.device_from_session(open_session("FAKE0001", adb)))
    fuse = next(c for c in report.checks if c.name == "Rollback fuse")
    assert fuse.status == pn.BLOCK and report.verdict == "BLOCKED"
