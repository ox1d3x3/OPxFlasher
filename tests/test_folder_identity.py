"""Loose-image folders (fastboot flasher kits): identity from signed AVB data,
and a clear message for a zip that holds images instead of an OTA."""

import zipfile

import pytest
import romfactory as rf

from opx.rom.ota import RomError, open_rom

FP_SYSTEM = "OPPO/PKJ110/OP5D55L1:16/BP2A.250605.015/V.R4T2.123456:user/release-keys"
FP_VENDOR = "OPPO/PKJ110/OP5D55L1:16/BP2A.250605.015/V.R4T2.123456:user/release-keys"


def make_folder(tmp_path, system_props=None, vendor_props=None, extra=("boot", "xbl_config", "system")):
    d = tmp_path / "OTA_FILES_HERE"
    d.mkdir()
    for name in extra:
        (d / f"{name}.img").write_bytes(b"\0" * 4096)
    (d / "vbmeta.img").write_bytes(rf.build_vbmeta(1, chains=[("vbmeta_system", 1), ("vbmeta_vendor", 2)]))
    if system_props is not None:
        (d / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props=system_props))
    if vendor_props is not None:
        (d / "vbmeta_vendor.img").write_bytes(rf.build_vbmeta(1, props=vendor_props))
    return d


def test_folder_identity_from_avb(tmp_path):
    d = make_folder(tmp_path,
                    {"com.android.build.system.fingerprint": FP_SYSTEM,
                     "com.android.build.system.security_patch": "2026-08-01"},
                    {"com.android.build.vendor.fingerprint": FP_VENDOR})
    rom = open_rom(d)
    assert rom.kind == "folder" and rom.codename == "OP5D55L1" and rom.product == "PKJ110"
    assert rom.android == "16" and rom.security_patch == "2026-08-01"
    assert rom.build_incremental == "V.R4T2.123456"
    assert any("signed AVB data in vbmeta_vendor" in n for n in rom.notes)  # the phone's own build first
    assert not any("can't be confirmed" in n for n in rom.notes)


def test_folder_disagreeing_devices_fail_closed(tmp_path):
    d = make_folder(tmp_path,
                    {"com.android.build.system.fingerprint": FP_SYSTEM},
                    {"com.android.build.vendor.fingerprint": FP_SYSTEM.replace("OP5D55L1", "OP5929L1")})
    rom = open_rom(d)
    assert rom.codename == "" and rom.fingerprint == ""
    assert any("more than one device" in n for n in rom.notes)


def test_folder_without_avb_properties(tmp_path):
    rom = open_rom(make_folder(tmp_path))
    assert rom.codename == "" and any("can't be confirmed" in n for n in rom.notes)


def test_flasher_kit_zip_is_read_directly(tmp_path):
    z = tmp_path / "Regional Flasher X9U CN 16.0.10.501.zip"
    vb = rf.build_vbmeta(1, props={"com.android.build.system.fingerprint": FP_SYSTEM})
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Regional Flasher X9U CN/Regional_Flasher.bat", "@echo off")
        zf.writestr("Regional Flasher X9U CN/Platform-Tools/readme.txt", "x")
        for name in ("boot", "system", "my_region"):
            zf.writestr(f"Regional Flasher X9U CN/OTA_FILES_HERE/{name}.img", b"\0" * 4096)
        zf.writestr("Regional Flasher X9U CN/OTA_FILES_HERE/vbmeta_system.img", vb)
    rom = open_rom(z)
    assert rom.kind == "image-zip" and rom.codename == "OP5D55L1"
    assert {i.name for i in rom.images} == {"boot", "system", "my_region", "vbmeta_system"}
    assert rom.image("system").logical and not rom.image("boot").logical and rom.image("system").guessed
    assert any("flasher package" in n for n in rom.notes)
    plain = tmp_path / "other.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(RomError, match="no payload.bin or images"):
        open_rom(plain)

def test_report_device_match_for_folder(tmp_path):
    from opx import plan as pn
    from opx import profile

    d = make_folder(tmp_path, {"com.android.build.system.fingerprint": FP_SYSTEM})
    rom = open_rom(d)
    dev = pn.DeviceState("live", "test", {"codename": "OP5D55L1", "model": "PKJ110"}, {}, profile.load("oplus-generic"))
    match = next(c for c in pn.build_report(rom, dev).checks if c.name == "Device match")
    assert match.status == pn.PASS
    dev.facts["codename"] = "dodge"
    match = next(c for c in pn.build_report(rom, dev).checks if c.name == "Device match")
    assert match.status == pn.BLOCK and "OP5D55L1" in match.detail


def test_chip_platform_build_is_not_an_identity(tmp_path):
    """OPPO kits: vbmeta_system carries Qualcomm's platform build (qti/canoe/canoe), shared by
    many phones. It must never become the ROM's identity."""
    d = tmp_path / "kit"
    d.mkdir()
    (d / "boot.img").write_bytes(b"\0" * 4096)
    platform = "qti/canoe/canoe:16/BP2A.250605.015/1785253342920:user/release-keys"
    (d / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props={
        "com.android.build.system.fingerprint": platform, "com.android.build.system.security_patch": "2026-08-01"}))
    rom = open_rom(d)
    assert rom.codename == "" and rom.product == "" and rom.fingerprint == ""
    assert any("only names the chip platform (canoe)" in n for n in rom.notes)

    # with the phone's own build next to it, that one is used
    (d / "vbmeta_vendor.img").write_bytes(rf.build_vbmeta(1, props={
        "com.android.build.vendor.fingerprint": FP_VENDOR, "com.android.build.vendor.security_patch": "2026-08-05"}))
    rom = open_rom(d)
    assert rom.codename == "OP5D55L1" and rom.product == "PKJ110" and rom.security_patch == "2026-08-05"
    assert any("vbmeta_vendor" in n for n in rom.notes)


def test_platform_only_kit_converts_to_a_folder_only(tmp_path):
    from opx import convert as cv

    d = tmp_path / "kit"
    d.mkdir()
    (d / "boot.img").write_bytes(b"\0" * 4096)
    (d / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props={
        "com.android.build.system.fingerprint": "qti/canoe/canoe:16/X/1:user/release-keys",
        "com.android.build.system.security_patch": "2026-08-01"}))
    with pytest.raises(cv.ConvertError, match="doesn't say which phone"):
        cv.convert(open_rom(d), tmp_path / "out")
    assert (cv.convert(open_rom(d), tmp_path / "out", make_zip=False, make_folder=True)["folder"] / "boot.img").exists()
