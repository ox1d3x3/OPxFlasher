"""Bootloader unlock: state checks, the maker's command, and the owner's confirmation."""

import json

import pytest

from opx import reboot as rb
from opx import unlock as ul
from opx.device import Device


@pytest.fixture
def locked_phone(fake_device, fb_state):
    props = json.loads((fake_device / "props.json").read_text())
    props.update({"ro.boot.verifiedbootstate": "green", "ro.boot.flash.locked": "1", "sys.oem_unlock_allowed": "1"})
    (fake_device / "props.json").write_text(json.dumps(props))
    fb_state(unlocked=False)
    return fake_device


def android():
    return Device(serial="FAKE0001", transport="adb", state="device")


def bootloader():
    return Device(serial="FAKE0001", transport="fastboot", state="bootloader")


def test_unlocks_after_the_owner_confirms(locked_phone, fb_state, adb, fastboot):
    said = []
    r = ul.run(android(), set(), adb=adb, fastboot=fastboot, say=said.append, wait_confirm=5)
    assert r.status == ul.UNLOCKED
    log = fb_state.read()["log"]
    assert "adb reboot bootloader" in log and "flashing unlock" in log
    assert not any("reboot fastboot" in x for x in log)  # never via fastbootd
    assert any("UNLOCK THE BOOTLOADER" in s for s in said) and "OEM unlocking is on." in said


def test_already_unlocked_from_android(fake_device, fb_state, adb, fastboot):
    r = ul.run(android(), set(), adb=adb, fastboot=fastboot, say=lambda s: None)  # fake phone is orange
    assert r.status == ul.ALREADY and fb_state.read()["log"] == []  # nothing rebooted or sent


def test_already_unlocked_from_the_bootloader(fake_device, fb_state, adb, fastboot):
    fb_state(mode="bootloader", unlocked=True)
    r = ul.run(bootloader(), None, adb=adb, fastboot=fastboot, say=lambda s: None)
    assert r.status == ul.ALREADY and "flashing unlock" not in fb_state.read()["log"]


def test_oem_unlocking_off_stops_before_rebooting(locked_phone, fb_state, adb, fastboot):
    props = json.loads((locked_phone / "props.json").read_text())
    props["sys.oem_unlock_allowed"] = "0"
    (locked_phone / "props.json").write_text(json.dumps(props))
    r = ul.run(android(), set(), adb=adb, fastboot=fastboot, say=lambda s: None)
    assert r.status == ul.NEEDS_OEM and "Developer options" in r.message
    assert fb_state.read()["log"] == []


def test_bootloader_refuses_when_oem_unlocking_is_off(locked_phone, fb_state, adb, fastboot):
    fb_state(mode="bootloader", oem_unlock_allowed=False)
    r = ul.run(bootloader(), None, adb=adb, fastboot=fastboot, say=lambda s: None)
    assert r.status == ul.NEEDS_OEM and "OEM unlocking" in r.message


def test_older_bootloaders_use_oem_unlock(locked_phone, fb_state, adb, fastboot):
    fb_state(mode="bootloader", unlock_style="oem")
    r = ul.run(bootloader(), None, adb=adb, fastboot=fastboot, say=lambda s: None, wait_confirm=5)
    assert r.status == ul.UNLOCKED and "oem unlock" in fb_state.read()["log"]


def test_waits_for_the_owner_then_says_what_to_do(locked_phone, fb_state, adb, fastboot):
    fb_state(mode="bootloader", owner_confirms=False)
    r = ul.run(bootloader(), None, adb=adb, fastboot=fastboot, say=lambda s: None, wait_confirm=0.2)
    assert r.status == ul.CONFIRM and "volume keys" in r.message


def test_bootloader_reboot_refused_where_it_bricks(locked_phone, fb_state, adb, fastboot):
    with pytest.raises(rb.RebootError):
        ul.run(android(), {"bootloader"}, adb=adb, fastboot=fastboot, say=lambda s: None)
    assert fb_state.read()["log"] == []


def test_oppo_note():
    assert "Deep Testing" in ul.oppo_note({"ro.product.brand": "OPPO"})
    assert ul.oppo_note({"ro.product.brand": "OnePlus"}) == ""


def test_cli_unlock_needs_the_sum_and_two_confirmations(locked_phone, fb_state, adb_env, monkeypatch, capsys):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd

    from opx import cli

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    monkeypatch.setattr("secrets.randbelow", lambda n: 0)  # 11 + 11
    answers = iter(["21"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["unlock"]) == 1 and "not right" in capsys.readouterr().out
    assert fb_state.read()["log"] == []
    answers = iter(["22", "I UNDERSTAND", "NO"])
    assert cli.main(["unlock"]) == 1 and fb_state.read()["log"] == []
    answers = iter(["22", "I UNDERSTAND", "YES"])
    assert cli.main(["unlock"]) == 0
    assert "flashing unlock" in fb_state.read()["log"] and "now unlocked" in capsys.readouterr().out
