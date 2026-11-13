"""USB driver / permission check with recorded system answers."""

from pathlib import Path

from opx import usbcheck as uc

OK_PHONE = {"Status": "OK", "Class": "AndroidUsbDeviceClass", "FriendlyName": "Android Composite ADB Interface",
            "InstanceId": r"USB\VID_22D9&PID_2769&MI_01\6&1"}
NO_DRIVER = {"Status": "Error", "Class": None, "FriendlyName": "PJZ110",
             "InstanceId": r"USB\VID_22D9&PID_2769\ABC"}
FASTBOOT_NO_DRIVER = {"Status": "Unknown", "Class": "Other", "FriendlyName": "Android",
                      "InstanceId": r"USB\VID_18D1&PID_D00D\X"}
EDL = {"Status": "OK", "Class": "Ports", "FriendlyName": "Qualcomm HS-USB QDLoader 9008 (COM5)",
       "InstanceId": r"USB\VID_05C6&PID_9008\5&2"}


def test_windows_missing_driver():
    found = uc.check_windows([NO_DRIVER])
    assert len(found) == 1 and found[0].level == "problem"
    assert "PJZ110" in found[0].message and "Android ADB Interface" in found[0].message


def test_windows_fastboot_missing_driver():
    msg = uc.check_windows([FASTBOOT_NO_DRIVER])[0].message
    assert "fastboot" in msg and "Android Bootloader Interface" in msg


def test_windows_all_good_and_edl():
    assert [f.level for f in uc.check_windows([OK_PHONE])] == ["ok"]
    found = uc.check_windows([EDL])
    assert found[0].level == "problem" and "9008" in found[0].message


def test_windows_powershell_output_parsing():
    class R:
        def __init__(self, out):
            self.stdout = out
    import json
    one = lambda *a, **k: R(json.dumps(NO_DRIVER))  # noqa: E731  (PowerShell prints an object, not a list, for one device)
    assert uc.windows_devices(run=one) == [NO_DRIVER]
    assert uc.windows_devices(run=lambda *a, **k: R("")) == []
    assert uc.windows_devices(run=lambda *a, **k: R("garbage")) == []

    def missing(*a, **k):
        raise FileNotFoundError("powershell")
    assert uc.windows_devices(run=missing) == []


def make_sysfs(root, entries):
    for i, (vid, pid) in enumerate(entries, 1):
        d = root / f"1-{i}"
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vid)
        (d / "idProduct").write_text(pid)
        (d / "busnum").write_text("1")
        (d / "devnum").write_text(str(i))
    (root / "usb1").mkdir()  # a hub without the files: ignored


def test_linux_permissions(tmp_path):
    make_sysfs(tmp_path, [("22d9", "2769"), ("8087", "0026")])
    devices = uc.linux_devices(tmp_path)
    assert devices == [{"vid": "22D9", "pid": "2769", "node": "/dev/bus/usb/001/001"}]
    assert uc.check_linux(devices, can_open=lambda n: False)[0].level == "problem"
    assert "plugdev" in uc.check_linux(devices, can_open=lambda n: False)[0].message
    assert uc.check_linux(devices, can_open=lambda n: True)[0].level == "ok"


def test_linux_edl(tmp_path):
    make_sysfs(tmp_path, [("05c6", "9008")])
    assert "9008" in uc.check_linux(uc.linux_devices(tmp_path))[0].message


def test_nothing_found_gives_cable_advice(monkeypatch):
    monkeypatch.setattr(uc.sys, "platform", "darwin")
    found = uc.check()
    assert found[0].level == "warning" and "data cable" in found[0].message


def test_cli_usb_check(monkeypatch, capsys):
    from opx import cli

    monkeypatch.setattr(uc, "check", lambda: [uc.Finding("problem", "fix the driver")])
    assert cli.main(["usb-check"]) == 1
    assert "[FIX ] fix the driver" in capsys.readouterr().out


def test_missing_driver_offers_fixes():
    f = uc.check_windows([NO_DRIVER])[0]
    assert f.actions == uc.DRIVER_ACTIONS
    assert "Windows Update" in f.message and "administrator" in f.message and "Device Manager" in f.message
    assert "Android ADB Interface" in f.message
    assert "Android Bootloader Interface" in uc.check_windows([FASTBOOT_NO_DRIVER])[0].message
    assert uc.check_windows([OK_PHONE])[0].actions == ()


def test_never_touches_windows_settings():
    src = (Path(uc.__file__)).read_text()
    for forbidden in ("ms-settings", "runas", "startfile", "ctypes", "winreg"):
        assert forbidden not in src, forbidden

