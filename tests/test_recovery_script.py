"""The OrangeFox / TWRP restore zip, including running its installer script
against a fake phone (partition files instead of block devices)."""

import os
import shutil
import subprocess
import zipfile

import pytest

from opx import restore as rs
from opx import restore_zip as rz
from test_flash import backup_of, phone  # noqa: F401  (fixture)
from test_restore_zip import REGION, SYSTEM, make_super

MB = 1 << 20
SH = shutil.which("dash") or shutil.which("sh")
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("unzip"),
                                reason="needs a POSIX shell and unzip")


@pytest.fixture
def big_phone(phone):
    # 3.5 MiB super so it's split into 4 parts of 1 MiB
    (phone / "by-name" / "super").write_bytes(make_super({"system": SYSTEM, "my_region": REGION}, 3 * MB + MB // 2))
    return phone


@pytest.fixture
def made(adb, big_phone, tmp_path):
    folder = backup_of(adb, tmp_path, "full")
    out = rz.build(folder, tmp_path / "out", recovery="orangefox", chunk=MB)
    return folder, out


def recovery(tmp_path, phone, props=None):
    """A copy of the phone's partitions plus a fake getprop."""
    block = tmp_path / "block"
    shutil.copytree(phone / "by-name", block)
    values = {"ro.product.device": "testdevice", "ro.serialno": "FAKE0001", "ro.boot.slot_suffix": "_a"}
    values.update(props or {})
    getprop = tmp_path / "getprop"
    getprop.write_text("#!/bin/sh\ncase \"$1\" in\n" + "".join(
        f"  {k}) echo '{v}' ;;\n" for k, v in values.items()) + "esac\n")
    getprop.chmod(0o755)
    return block, getprop


def install(zip_path, tmp_path, block, getprop):
    script = tmp_path / "update-binary"
    with zipfile.ZipFile(zip_path) as z:
        script.write_bytes(z.read("META-INF/com/google/android/update-binary"))
    env = dict(os.environ, OPX_BLOCK_DIR=str(block), OPX_GETPROP=str(getprop),
               OPX_CMDLINE=str(tmp_path / "nocmdline"), OPX_METADATA=str(tmp_path / "metadata"),
               OPX_TMP=str(tmp_path / "rtmp"))
    res = subprocess.run([SH, str(script), "3", "", str(zip_path)], env=env, capture_output=True, text=True)
    return res.returncode, res.stdout + res.stderr


def snapshot(block):
    return {p.name: p.read_bytes() for p in block.iterdir()}


def test_zip_layout(made):
    _, out = made
    assert out.name.endswith("-orangefox.zip")
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"META-INF/com/google/android/update-binary", "opx/restore.list", "boot.img",
                "super.img.000", "super.img.003"} <= names
        assert "super.img.004" not in names and "android-info.txt" not in names
        for never in ("persist.img", "userdata.img", "metadata.img", "modemst1.img"):
            assert never not in names
        listing = z.read("opx/restore.list").decode()
        assert "serial FAKE0001" in listing and "slot _a" in listing
        assert "image xbl_config xbl_config.img" in listing and " firmware" in listing


def test_install_restores_the_phone(made, tmp_path, big_phone):
    _, out = made
    block, getprop = recovery(tmp_path, big_phone)
    want = snapshot(block)
    # break the phone: wipe boot_a and scribble over super
    (block / "boot_a").write_bytes(b"\0" * len(want["boot_a"]))
    (block / "super").write_bytes(b"\xff" * len(want["super"]))
    code, log = install(out, tmp_path, block, getprop)
    assert code == 0, log
    assert "Restore finished" in log
    now = snapshot(block)
    assert now["boot_a"] == want["boot_a"] and now["super"] == want["super"]
    assert now["boot_b"] == want["boot_b"]  # the other slot is never written


@pytest.mark.parametrize("props,why", [
    ({"ro.serialno": "OTHER999"}, "another phone"),
    ({"ro.product.device": "lemonade"}, "this phone is 'lemonade'"),
    ({"ro.boot.slot_suffix": "_b"}, "Switch slot"),
    ({"ro.serialno": ""}, "Can't read this phone's serial"),
])
def test_refuses_other_phone_or_slot(made, tmp_path, big_phone, props, why):
    _, out = made
    block, getprop = recovery(tmp_path, big_phone, props)
    before = snapshot(block)
    code, log = install(out, tmp_path, block, getprop)
    assert code == 1 and why in log and "Nothing was written" in log
    assert snapshot(block) == before


def test_serial_from_kernel_cmdline(made, tmp_path, big_phone):
    _, out = made
    block, getprop = recovery(tmp_path, big_phone, {"ro.serialno": ""})
    (tmp_path / "nocmdline").write_text("console=null androidboot.serialno=FAKE0001 quiet\n")
    code, log = install(out, tmp_path, block, getprop)
    assert code == 0, log


def test_refuses_when_firmware_changed(made, tmp_path, big_phone):
    _, out = made
    block, getprop = recovery(tmp_path, big_phone)
    data = bytearray((block / "xbl_config_a").read_bytes())
    data[-1] ^= 1  # an update changed the bootloader chain
    (block / "xbl_config_a").write_bytes(bytes(data))
    before = snapshot(block)
    code, log = install(out, tmp_path, block, getprop)
    assert code == 1 and "anti-rollback" in log and snapshot(block) == before


def test_refuses_during_update_merge(made, tmp_path, big_phone):
    _, out = made
    block, getprop = recovery(tmp_path, big_phone)
    (tmp_path / "metadata" / "ota" / "snapshots").mkdir(parents=True)
    (tmp_path / "metadata" / "ota" / "snapshots" / "system_b").write_text("x")
    code, log = install(out, tmp_path, block, getprop)
    assert code == 1 and "merge in progress" in log


def test_damaged_zip_writes_nothing(made, tmp_path, big_phone):
    _, out = made
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(damaged, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "super.img.002":
                data = b"\x01" + data[1:]
            dst.writestr(item, data)
    block, getprop = recovery(tmp_path, big_phone)
    before = snapshot(block)
    code, log = install(damaged, tmp_path, block, getprop)
    assert code == 1 and "super.img.002 in the zip is damaged" in log and snapshot(block) == before


def test_build_refuses_unsafe_values_and_bad_chunk(adb, big_phone, tmp_path):
    folder = backup_of(adb, tmp_path, "full")
    with pytest.raises(rs.RestoreError, match="whole MiB"):
        rz.build(folder, tmp_path / "out", recovery="orangefox", chunk=1000)
    with pytest.raises(rs.RestoreError, match="Unknown recovery"):
        rz.build(folder, tmp_path / "out", recovery="cwm")
    m = rs.manifest_of(folder)
    m["device"]["serial"] = "FAKE 0001; reboot"
    import json
    (folder / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(rs.RestoreError, match="serial number"):
        rz.build(folder, tmp_path / "out", recovery="orangefox")


def test_cli_orangefox(adb, big_phone, tmp_path, capsys):
    from opx import cli

    folder = backup_of(adb, tmp_path, "full")
    assert cli.main(["restore-zip", str(folder), "--recovery", "orangefox", "--out", str(tmp_path / "z")]) == 0
    assert "OrangeFox / TWRP" in capsys.readouterr().out
    assert len(list((tmp_path / "z").glob("*-orangefox.zip"))) == 1
