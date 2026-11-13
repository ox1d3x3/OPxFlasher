from opx import cli
from opx import device as dev

ADB_OUT = """List of devices attached
ABC123         device usb:1-1 product:PJZ110 model:PJZ110 device:testdevice transport_id:3
DEF456         unauthorized usb:1-2 transport_id:4
GHI789         recovery product:twrp model:OnePlus_13 device:testdevice transport_id:5
"""

GETVAR = """(bootloader) version-bootloader:abl-1.0
(bootloader) product:testdevice
(bootloader) unlocked:yes
(bootloader) current-slot:a
(bootloader) is-userspace:no
(bootloader) partition-size:boot_a: 0x6000000
all:
Finished. Total time: 0.050s
"""


def test_parse_adb_devices():
    devices = dev.parse_adb_devices(ADB_OUT)
    assert [(d.serial, d.state) for d in devices] == [
        ("ABC123", "device"),
        ("DEF456", "unauthorized"),
        ("GHI789", "recovery"),
    ]
    assert devices[0].model == "PJZ110" and devices[0].can_backup
    assert not devices[1].can_backup
    assert devices[2].model == "OnePlus 13" and devices[2].can_backup


def test_parse_fastboot():
    assert dev.parse_fastboot_devices("XYZ\tfastboot\n") == ["XYZ"]
    info = dev.parse_getvar_all(GETVAR)
    assert info["unlocked"] == "yes" and info["current-slot"] == "a"
    assert info["partition-size"].startswith("boot_a")


def test_cli_devices_and_dry_run(adb_env, capsys, tmp_path):
    assert cli.main(["devices"]) == 0
    assert "FAKE0001" in capsys.readouterr().out

    assert cli.main(["backup", "--mode", "network", "--dry-run", "--out", str(tmp_path / "o")]) == 0
    out = capsys.readouterr().out
    assert "oplusstanvbk_a" in out and "Dry run" in out
    assert not (tmp_path / "o").exists()


def test_cli_backup_and_verify(adb_env, capsys, tmp_path):
    assert cli.main(["backup", "--mode", "network", "--out", str(tmp_path / "o")]) == 0
    folder = next((tmp_path / "o").iterdir())
    assert cli.main(["verify", str(folder)]) == 0
    assert "3 OK, 0 problem(s)" in capsys.readouterr().out


def test_cli_info_shows_warning_for_cos17(adb_env, capsys):
    import json

    props_file = adb_env / "props.json"
    props = json.loads(props_file.read_text())
    props["ro.build.display.id"] = "PJZ110_17.0.0.120(CN01)"
    props_file.write_text(json.dumps(props))
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "DANGER" in out and "fastboot" in out


def test_cli_unauthorized(adb_env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_STATE", "unauthorized")
    try:
        cli.main(["backup", "--dry-run"])
    except SystemExit as e:
        assert "not authorised" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_tool_override_keeps_paths_with_spaces(monkeypatch, tmp_path):
    # Windows user folders often contain spaces ("C:\Users\First Last\...").
    from conftest import tool_cmd

    from opx.tools import find_tool

    exe = tmp_path / "Program Files" / "python.exe"
    script = tmp_path / "My Tools" / "fake adb.py"
    monkeypatch.setenv("OPX_ADB", tool_cmd(exe, script, "-c"))
    assert find_tool("adb") == [str(exe), str(script), "-c"]


def test_platform_tools_download_covers_macos(monkeypatch, tmp_path):
    from opx import tools

    assert tools.GOOGLE_PLATFORM_TOOLS["darwin"].endswith("platform-tools-latest-darwin.zip")
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    monkeypatch.setattr(tools.os, "name", "posix")
    monkeypatch.setattr(tools.Path, "home", lambda: tmp_path)
    assert tools.user_tools_dir() == tmp_path / "Library" / "Application Support" / "OPxFlasher" / "platform-tools"


def test_tools_next_to_appimage_are_found(monkeypatch, tmp_path):
    from opx import tools

    image = tmp_path / "OPxFlasher.AppImage"
    image.write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert tmp_path / "platform-tools" / f"adb{tools._EXE}" in tools._candidates("adb")
