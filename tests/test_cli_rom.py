import json

import romfactory as rf
from test_plan import put, rom_zip, sized

from opx import cli


def test_rom_inspect_extract_avb_arb(tmp_path, capsys):
    rom = rom_zip(tmp_path)
    assert cli.main(["rom", "inspect", str(rom.path)]) == 0
    out = capsys.readouterr().out
    assert "testdevice" in out and "my_region" in out and "logical" in out

    assert cli.main(["rom", "extract", str(rom.path), "vbmeta", "xbl_config", "--out", str(tmp_path / "x")]) == 0
    assert (tmp_path / "x" / "vbmeta.img").exists()

    assert cli.main(["rom", "avb", str(tmp_path / "x" / "vbmeta.img")]) == 0
    assert "rollback index     2" in capsys.readouterr().out

    assert cli.main(["rom", "arb", str(tmp_path / "x" / "xbl_config.img")]) == 0
    out = capsys.readouterr().out
    assert "EXPERIMENTAL" in out and "OEM ARB             1" in out


def test_plan_offline_is_blocked(tmp_path, capsys):
    rom = rom_zip(tmp_path)
    assert cli.main(["plan", str(rom.path), "--offline"]) == 3
    assert "Verdict: BLOCKED" in capsys.readouterr().out


def test_plan_live_json(adb_env, tmp_path, capsys):
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(adb_env, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(adb_env, f"dtbo{slot}", b"\0" * 65536)
    rom = rom_zip(tmp_path)
    code = cli.main(["plan", str(rom.path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 3 and report["verdict"] == "BLOCKED"
    names = {c["name"]: c["status"] for c in report["checks"]}
    assert names["Device match"] == "pass" and names["AVB vbmeta"] == "pass"


def test_bad_rom_path_is_a_clean_error(tmp_path, capsys):
    assert cli.main(["rom", "inspect", str(tmp_path / "missing.zip")]) == 2
    assert "Error:" in capsys.readouterr().err
