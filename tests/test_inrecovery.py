"""Route B: copy a recovery zip to the phone, check it there, restart into AERA, read the install's result."""

import json
import zipfile
from types import SimpleNamespace

import pytest

from opx import inrecovery as ir


@pytest.fixture
def aera_phone(fake_device, monkeypatch):
    monkeypatch.setenv("FAKE_AERA", "1")
    (fake_device / "fastboot.json").write_text(json.dumps({"mode": "android"}))
    return fake_device


def recovery_zip(tmp_path, keep=True, cnkeep=""):
    z = tmp_path / "CPH2653-17-fullrom-GLO-keeprec.zip"
    lines = ["product CPH2653"] + (["keeprecovery"] if keep else []) + ([f"cnkeep {cnkeep}"] if cnkeep else [])
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("opx/rom.list", "\n".join(lines) + "\n")
        zf.writestr("payload.bin", bytes(range(256)) * 4096)
    return z


def runner(adb, tmp_path, z, events=None, **kw):
    rom = SimpleNamespace(path=z, kind="fullrom")
    opts = ir.Options(**{k: kw.pop(k) for k in ("reboot_after", "delete_after", "hardware") if k in kw})
    return ir.Runner(rom, adb, "FAKE0001", tmp_path / "out", tmp_path / "work", opts,
                     on_event=(events.append if events is not None else None), poll=0.01,
                     recovery_timeout=kw.pop("recovery_timeout", 5), install_timeout=kw.pop("install_timeout", 5))


def mode(fake_device):
    return json.loads((fake_device / "fastboot.json").read_text())["mode"]


def test_copy_check_recovery_install_reboot(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AERA_RESULT", "ok")
    z = recovery_zip(tmp_path)
    events = []
    out = runner(adb, tmp_path, z, events).run()
    assert out.result == "ok" and out.rebooted and out.zip_path == z  # a zip that keeps AERA is used as it is
    assert mode(aera_phone) == "android"  # restarted to Android after the install
    assert not (aera_phone / "sdcard" / "OPxFlasher" / z.name).exists()  # ~10 GB freed again
    assert (tmp_path / "work" / "opx-install.log").exists()
    log = (tmp_path / "commands.log").read_text()
    assert log.index(f"push /sdcard/OPxFlasher/{z.name}") < log.index(f"sha256sum '/sdcard/OPxFlasher/{z.name}'")
    assert any(e["type"] == "on_phone" and "Install > Internal storage > OPxFlasher" in e["message"] for e in events)
    assert [e["index"] for e in events if e["type"] == "step_start"] == [1, 2, 3, 4, 5, 6]
    assert any(e["type"] == "transfer" and e["done"] == e["total"] for e in events)


def test_a_dropped_copy_is_copied_again(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AERA_RESULT", "ok")
    monkeypatch.setenv("FAKE_PUSH_DROP", "1")
    z = recovery_zip(tmp_path)
    out = runner(adb, tmp_path, z, reboot_after=False, delete_after=False).run()
    assert out.result == "ok" and not out.rebooted
    assert (aera_phone / "sdcard" / "OPxFlasher" / z.name).read_bytes() == z.read_bytes()
    assert (tmp_path / "commands.log").read_text().count(f"push /sdcard/OPxFlasher/{z.name}") == 2


def test_a_damaged_copy_never_reaches_the_recovery(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PUSH_CORRUPT", "1")
    z = recovery_zip(tmp_path)
    with pytest.raises(ir.InRecoveryError, match="damaged"):
        runner(adb, tmp_path, z).run()
    assert mode(aera_phone) == "android"  # never restarted
    assert not (aera_phone / "sdcard" / "OPxFlasher" / z.name).exists()


def test_not_enough_space(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FREE_KB", "10")
    with pytest.raises(ir.InRecoveryError, match="Not enough free space"):
        runner(adb, tmp_path, recovery_zip(tmp_path)).run()


def test_already_copied_is_not_copied_again(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AERA_RESULT", "ok")
    z = recovery_zip(tmp_path)
    target = aera_phone / "sdcard" / "OPxFlasher" / z.name
    target.parent.mkdir(parents=True)
    target.write_bytes(z.read_bytes())
    runner(adb, tmp_path, z).run()
    assert "push" not in (tmp_path / "commands.log").read_text()


def test_stock_recovery_means_aera_isnt_installed(adb, fake_device, tmp_path, monkeypatch):
    (fake_device / "fastboot.json").write_text(json.dumps({"mode": "android"}))  # no FAKE_AERA: no adb there
    with pytest.raises(ir.InRecoveryError, match="AERA isn't installed"):
        runner(adb, tmp_path, recovery_zip(tmp_path), recovery_timeout=0.2).run()


@pytest.mark.parametrize("line, match", [
    ("fail nothing-written: This ROM is OLDER than the installed Android", "before writing anything"),
    ("fail half-written: odm doesn't match", "Do NOT reboot"),
])
def test_a_failed_install_keeps_the_zip_and_doesnt_reboot(adb, aera_phone, tmp_path, monkeypatch, line, match):
    monkeypatch.setenv("FAKE_AERA_RESULT", line)
    z = recovery_zip(tmp_path)
    with pytest.raises(ir.InRecoveryError, match=match):
        runner(adb, tmp_path, z).run()
    assert mode(aera_phone) == "recovery"
    assert (aera_phone / "sdcard" / "OPxFlasher" / z.name).exists()  # install it again from the recovery


def test_no_result_is_never_called_success(adb, aera_phone, tmp_path):
    # an older zip (no result line) or nobody pressed Install: say so, don't guess
    with pytest.raises(ir.InRecoveryError, match="No result"):
        runner(adb, tmp_path, recovery_zip(tmp_path), install_timeout=0.3).run()


def test_format_data_result_doesnt_reboot(adb, aera_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AERA_RESULT", "ok format-data")
    out = runner(adb, tmp_path, recovery_zip(tmp_path)).run()
    assert out.needs_format and not out.rebooted and mode(aera_phone) == "recovery"


def test_stop_while_waiting(adb, aera_phone, tmp_path):
    r = runner(adb, tmp_path, recovery_zip(tmp_path), install_timeout=30)
    r.emit = lambda e: r.cancel.set() if e["type"] == "on_phone" and "Install" in e["message"] else None
    with pytest.raises(ir.InRecoveryError, match="let it finish"):
        r.run()


def test_zip_keeps_recovery(tmp_path):
    assert ir.zip_keeps_recovery(recovery_zip(tmp_path, cnkeep="abl engineering_cdt")) == (True,
                                                                                           "abl engineering_cdt")
    assert ir.zip_keeps_recovery(recovery_zip(tmp_path, keep=False))[0] is False
    assert ir.zip_keeps_recovery(tmp_path / "missing.zip") == (False, "")


def test_free_space_parsing():
    assert ir._free_bytes("Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse 100 1 50 1% /x\n") \
        == 50 * 1024
    assert ir._free_bytes("Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block/very-long-name\n"
                          "   100 1 77 1% /x\n") == 77 * 1024
    assert ir._free_bytes("") is None
