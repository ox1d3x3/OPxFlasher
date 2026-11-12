"""Full ROM restore from a Full firmware backup, on the simulated phone."""

import json

import pytest
from test_flash import backup_of, phone  # noqa: F401  (fixture)

from opx import flash as fl
from opx import plan as pn
from opx import restore as rs
from opx.session import open_session


def full_backup(adb, tmp_path):
    return backup_of(adb, tmp_path, "full")


def live(adb):
    s = open_session("FAKE0001", adb)
    return s, pn.device_from_session(s)


def run(plan, rom, profile, adb, fastboot, tmp_path):
    fl.Runner(plan, rom, profile, adb, fastboot, tmp_path / "j.json", tmp_path / "w", wait_timeout=5).run()


def test_backup_becomes_a_rom_of_the_active_slot(adb, phone, tmp_path):
    folder = full_backup(adb, tmp_path)
    rom = rs.backup_as_rom(folder)
    names = {i.name for i in rom.images}
    assert "super" in names and "boot" in names and "vbmeta" in names
    assert not any(n.endswith(("_a", "_b")) for n in names)
    assert not names & {"userdata", "metadata", "frp", "persist", "modemst1"}
    assert rom.image("boot").file.name == "boot_a.img"  # slot a was active
    assert rom.codename == "testdevice" and rom.security_patch == "2026-09-01"


def test_restore_brings_the_os_back(adb, fastboot, phone, fb_state, tmp_path):
    folder = full_backup(adb, tmp_path)
    good_boot = (phone / "by-name" / "boot_a").read_bytes()
    good_super = (phone / "by-name" / "super").read_bytes()
    (phone / "by-name" / "boot_a").write_bytes(b"\0" * len(good_boot))  # the ROM "breaks"
    (phone / "by-name" / "super").write_bytes(b"\1" * len(good_super))

    s, device = live(adb)
    rom, report, plan = rs.prepare_restore_rom(folder, device, live=True, wipe=False, overrides=[])
    assert plan.ok, plan.problems
    assert any(c.name == "Same phone" and c.status == "pass" for c in report.checks)
    kinds = [st.kind for st in plan.steps]
    assert "flash-super" in kinds and "to-fastbootd" not in kinds  # super from the bootloader
    assert not any(st.partition in ("userdata", "metadata", "persist") for st in plan.steps)

    run(plan, rom, s.profile, adb, fastboot, tmp_path)
    assert (phone / "by-name" / "boot_a").read_bytes() == good_boot
    assert (phone / "super.flashed").read_bytes() == good_super  # the fake records super flashes here
    assert fb_state.read().get("wiped") is not True  # data kept


def test_only_the_same_phone(adb, phone, tmp_path):
    folder = full_backup(adb, tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["device"]["serial"] = "SOMEONEELSE"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    _, device = live(adb)
    _, report, plan = rs.prepare_restore_rom(folder, device, True, False, [])
    assert not plan.ok and any("another phone" in p for p in plan.problems)


def test_phone_in_fastboot_needs_not_updated_confirmation(adb, fastboot, phone, fb_state, tmp_path):
    folder = full_backup(adb, tmp_path)
    device = rs.device_in_fastboot(folder, "FAKE0001")
    _, _, plan = rs.prepare_restore_rom(folder, device, live=False, wipe=False, overrides=[])
    assert not plan.ok and any("Current firmware" in p for p in plan.problems)
    _, _, plan = rs.prepare_restore_rom(folder, device, False, False, [fl.OVERRIDE_UNKNOWN_FIRMWARE])
    assert plan.ok, plan.problems

    # a different phone in fastboot still fails the serial check
    other = rs.device_in_fastboot(folder, "OTHER123")
    _, _, plan = rs.prepare_restore_rom(folder, other, False, False, [fl.OVERRIDE_UNKNOWN_FIRMWARE])
    assert any("another phone" in p for p in plan.problems)


def test_older_backup_needs_a_wipe(adb, phone, tmp_path):
    folder = full_backup(adb, tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["device"]["security_patch"] = "2025-01-01"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    _, device = live(adb)
    _, _, plan = rs.prepare_restore_rom(folder, device, True, False, [])
    assert any("needs a data wipe" in p for p in plan.problems)
    _, _, plan = rs.prepare_restore_rom(folder, device, True, True, [])
    assert not any("needs a data wipe" in p for p in plan.problems)


def test_only_full_finished_backups(adb, phone, tmp_path):
    with pytest.raises(rs.RestoreError, match="Full firmware"):
        rs.backup_as_rom(backup_of(adb, tmp_path, "critical"))
    with pytest.raises(rs.RestoreError, match="not an OPxFlasher backup"):
        rs.backup_as_rom(tmp_path)


def test_cli_restore_rom(adb_env, adb, fastboot, phone, fb_state, tmp_path, monkeypatch, capsys):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_flash_cli import answers

    from opx import cli

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    folder = full_backup(adb, tmp_path)
    good_boot = (phone / "by-name" / "boot_a").read_bytes()
    (phone / "by-name" / "boot_a").write_bytes(b"\0" * len(good_boot))
    args = ["restore-rom", str(folder), "--work", str(tmp_path / "w")]
    assert cli.main(args + ["--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    answers(monkeypatch, "no")
    assert cli.main(args) == 1
    answers(monkeypatch, "RESTORE ROM")
    assert cli.main(args) == 0
    assert (phone / "by-name" / "boot_a").read_bytes() == good_boot
