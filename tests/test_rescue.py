"""'My phone won't start' assistant."""

from opx import cli, rescue
from opx.device import Device
from test_flash import phone  # noqa: F401  (fixture)


def actions(a):
    return [s.action for s in a.steps]


ANDROID = Device("FAKE0001", "adb", "device")
RECOVERY = Device("FAKE0001", "adb", "recovery")
BOOTLOADER = Device("FAKE0001", "fastboot", "bootloader")


def test_states():
    a = rescue.assess([BOOTLOADER], False, True)
    assert a.state == "bootloader" and "restore-rom" in actions(a) and "reboot:recovery" in actions(a)
    a = rescue.assess([BOOTLOADER], False, False)
    assert "official-roms" in actions(a) and "restore-rom" not in actions(a)

    a = rescue.assess([RECOVERY], False, True)
    assert a.state == "recovery" and "restore-zip" in actions(a)
    assert "restore-zip" not in actions(rescue.assess([RECOVERY], False, False))

    assert rescue.assess([ANDROID], False, True, boot_completed="1").state == "android"
    a = rescue.assess([ANDROID], False, True, boot_completed="")
    assert a.state == "bootloop" and actions(a)[0] == "save-logs"

    assert rescue.assess([Device("X", "adb", "unauthorized")], False, False).state == "unauthorized"

    a = rescue.assess([], True, True)
    assert a.state == "edl" and "identify-9008" in actions(a)
    assert any("authorised" in s.text for s in a.steps)
    assert not any(s.action.startswith("flash") for s in a.steps)

    a = rescue.assess([], False, False)
    assert a.state == "none" and actions(a)[0] == "usb-check"


def test_known_issue_warns_in_fastboot():
    a = rescue.assess([BOOTLOADER], False, True, blocked={"bootloader"})
    assert "known problem" in a.steps[0].text


def test_gather_with_fake_phone(adb, fastboot, fb_state, tmp_path, monkeypatch):
    from opx import sahara

    fb_state(mode="bootloader")
    monkeypatch.setattr(sahara, "find_ports", lambda: [])
    a = rescue.gather([tmp_path], adb, fastboot)
    assert a.state == "bootloader" and "official-roms" in actions(a)


def test_gather_android(adb, fastboot, phone, tmp_path, monkeypatch):
    from opx import sahara

    monkeypatch.setattr(sahara, "find_ports", lambda: [])
    a = rescue.gather([tmp_path], adb, fastboot)
    assert a.state == "bootloop"  # the fake phone doesn't report sys.boot_completed


def test_cli_rescue(monkeypatch, capsys):
    monkeypatch.setattr(rescue, "gather", lambda roots: rescue.assess([], True, False))
    assert cli.main(["rescue"]) == 0
    out = capsys.readouterr().out
    assert "9008" in out and "opx edl-info" in out
