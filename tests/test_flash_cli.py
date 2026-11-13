import builtins
import json
import sys

import pytest
import romfactory as rf
from conftest import FAKE_FASTBOOT, tool_cmd
from test_flash import ROM_PARTS, make_rom
from test_plan import put, sized

from opx import cli


@pytest.fixture
def ready(adb_env, fb_state, monkeypatch, tmp_path):
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(adb_env, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(adb_env, f"dtbo{slot}", b"\0" * 65536)
    assert cli.main(["backup", "--mode", "critical", "--out", str(tmp_path / "b")]) == 0
    backup = next((tmp_path / "b").iterdir())
    rom = make_rom(tmp_path)
    return adb_env, fb_state, rom, backup


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))


def test_flash_blocked_without_override(ready, capsys):
    phone, state, rom, backup = ready
    assert cli.main(["flash", str(rom.path), "--backup", str(backup), "--allow-fastbootd"]) == 3
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "--accept-unvalidated-arb" in out
    assert state.read()["log"] == []


def test_flash_dry_run(ready, capsys):
    phone, state, rom, backup = ready
    code = cli.main(["flash", str(rom.path), "--backup", str(backup), "--allow-fastbootd", "--accept-unvalidated-arb", "--dry-run"])
    assert code == 0 and "Dry run" in capsys.readouterr().out
    assert state.read()["log"] == []


def test_flash_needs_both_typed_confirmations(ready, monkeypatch, tmp_path):
    phone, state, rom, backup = ready
    args = ["flash", str(rom.path), "--backup", str(backup), "--allow-fastbootd", "--accept-unvalidated-arb", "--work", str(tmp_path / "w")]
    answers(monkeypatch, "ARB CHECKED", "flash")  # lower case is not accepted
    assert cli.main(args) == 1
    assert state.read()["log"] == []

    answers(monkeypatch, "ARB CHECKED", "FLASH")
    assert cli.main(args) == 0
    assert state.read()["log"][-1] == "reboot"
    assert (phone / "by-name" / "dtbo_b").read_bytes().startswith(ROM_PARTS["dtbo"])


def test_flash_failure_then_resume(ready, monkeypatch, tmp_path, capsys):
    phone, state, rom, backup = ready
    state(fail_flash="dtbo_a")
    answers(monkeypatch, "ARB CHECKED", "FLASH")
    args = ["flash", str(rom.path), "--backup", str(backup), "--allow-fastbootd", "--accept-unvalidated-arb", "--work", str(tmp_path / "w")]
    assert cli.main(args) == 4
    assert "Do not reboot" in capsys.readouterr().err
    journal = next((tmp_path / "w").iterdir()) / "journal.json"
    steps = json.loads(journal.read_text())["steps"]
    assert next(st for st in steps if st["partition"] == "dtbo")["status"] == "failed"

    state(fail_flash=None)
    answers(monkeypatch, "RESUME")
    assert cli.main(["flash", "--resume", str(journal)]) == 0
    assert state.read()["log"][-1] == "reboot"
