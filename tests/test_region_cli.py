from test_flash_cli import answers, ready  # noqa: F401  (fixture)
from test_region import IN_PARTS, rom_for, simulate_ota_overwrite

from opx import cli


def test_region_show(ready, capsys):
    phone, state, rom, backup = ready
    assert cli.main(["region", "show", "--backup", str(backup)]) == 0
    out = capsys.readouterr().out
    assert "OK   oplusstanvbk_a" in out and "my_region_a" in out and "logical" in out


def test_region_restore_after_ota(ready, monkeypatch, tmp_path, capsys):
    phone, state, rom, backup = ready
    original = (phone / "mapper" / "my_region_a").read_bytes()
    simulate_ota_overwrite(phone)

    assert cli.main(["region", "restore", "--backup", str(backup)]) == 3  # my_region needs fastbootd
    assert "needs fastbootd" in capsys.readouterr().out

    answers(monkeypatch, "restore")  # wrong word cancels
    assert cli.main(["region", "restore", "--backup", str(backup), "--allow-fastbootd"]) == 1
    assert state.read()["log"] == []

    answers(monkeypatch, "RESTORE")
    assert cli.main(["region", "restore", "--backup", str(backup), "--allow-fastbootd", "--work", str(tmp_path / "w")]) == 0
    assert (phone / "mapper" / "my_region_a").read_bytes() == original
    assert state.read()["log"][-1] == "reboot"


def test_region_restore_only_physical(ready, monkeypatch, tmp_path):
    phone, state, rom, backup = ready
    answers(monkeypatch, "RESTORE")
    args = ["region", "restore", "--backup", str(backup), "--only", "oplusstanvbk", "--work", str(tmp_path / "w")]
    assert cli.main(args) == 0  # no fastbootd needed
    assert "reboot fastboot" not in state.read()["log"]


def test_flash_conversion_restores_region_by_default(ready, monkeypatch, tmp_path, capsys):
    phone, state, _, backup = ready
    original = (phone / "by-name" / "oplusstanvbk_a").read_bytes()
    rom = rom_for(tmp_path, parts=IN_PARTS)
    base = ["flash", str(rom.path), "--backup", str(backup), "--allow-fastbootd", "--accept-unvalidated-arb", "--work", str(tmp_path / "w")]

    assert cli.main(base + ["--wipe"]) == 3  # another variant needs --accept-variant-change
    assert "--accept-variant-change" in capsys.readouterr().out
    assert state.read()["log"] == []

    base += ["--accept-variant-change"]
    assert cli.main(base) == 3  # conversion without --wipe is blocked
    assert "different region" in capsys.readouterr().out

    answers(monkeypatch, "no")  # the variant question defaults to no
    assert cli.main(base + ["--wipe"]) == 1
    assert state.read()["log"] == []

    answers(monkeypatch, "CHANGE VARIANT", "ARB CHECKED", "FLASH")
    assert cli.main(base + ["--wipe"]) == 0
    out = capsys.readouterr().out
    assert "Region change: your phone's own oplusstanvbk, my_region will be restored" in out
    assert (phone / "by-name" / "oplusstanvbk_a").read_bytes() == original
    assert state.read()["wiped"] is True
