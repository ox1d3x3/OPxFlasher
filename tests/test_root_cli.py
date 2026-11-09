import json

from opx import cli
from test_flash_cli import answers, ready  # noqa: F401  (fixture)
from test_root import FINGERPRINT, STOCK, magisk_patch, rom_with_init_boot


def make_rootable(phone):
    for slot in ("_a", "_b"):
        (phone / "by-name" / f"init_boot{slot}").write_bytes(STOCK + b"\0" * (65536 - len(STOCK)))
    props = json.loads((phone / "props.json").read_text())
    props["ro.build.fingerprint"] = FINGERPRINT
    (phone / "props.json").write_text(json.dumps(props))


def test_root_cli_flow(ready, monkeypatch, tmp_path, capsys):
    phone, state, _, backup = ready
    make_rootable(phone)
    rom = rom_with_init_boot(tmp_path)
    work = tmp_path / "rootwork"

    assert cli.main(["root", "prepare", "--manager", "magisk", "--rom", str(rom.path), "--work", str(work)]) == 0
    out = capsys.readouterr().out
    assert "Select and Patch a File" in out and "opx_stock_init_boot.img" in out

    assert cli.main(["root", "pull", "--work", str(work)]) == 2  # not patched yet
    assert "No new patched image" in capsys.readouterr().err

    patched = magisk_patch(phone)
    assert cli.main(["root", "pull", "--work", str(work)]) == 0
    assert "Checks passed" in capsys.readouterr().out

    assert cli.main(["root", "flash", "--work", str(work)]) == 3  # no backup given
    answers(monkeypatch, "ROOT")
    assert cli.main(["root", "flash", "--work", str(work), "--backup", str(backup)]) == 0
    assert (phone / "by-name" / "init_boot_a").read_bytes().startswith(patched)

    state(mode="android")
    answers(monkeypatch, "UNROOT")
    assert cli.main(["root", "unroot", "--work", str(work), "--backup", str(backup)]) == 0
    assert (phone / "by-name" / "init_boot_a").read_bytes().startswith(STOCK)


def test_root_prepare_refuses_other_build(ready, tmp_path, capsys):
    phone, *_ = ready
    make_rootable(phone)
    rom = rom_with_init_boot(tmp_path, fingerprint_product="CPH2649")
    assert cli.main(["root", "prepare", "--manager", "kernelsu", "--rom", str(rom.path), "--work", str(tmp_path / "w")]) == 2
    assert "bootloop" in capsys.readouterr().err
    assert not (phone / "sdcard" / "Download" / "opx_stock_init_boot.img").exists()
