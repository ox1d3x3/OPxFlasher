"""The full-ROM installer (opx/recovery/rom-install.sh) run under a POSIX shell
against a simulated phone: partition files for /dev/block/by-name, a mapper
folder for logical partitions, a fake lptools and the installed build.prop."""

import os
import shutil
import subprocess
import zipfile

import pytest
from test_convert import LOGICAL, make_ota, padded

from opx import convert as cv
from opx.rom.ota import open_rom

MB = 1 << 20
SH = shutil.which("dash") or shutil.which("sh")
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("unzip"),
                                reason="needs a POSIX shell and unzip")

INSTALLED = {"ro.system.build.fingerprint": "OnePlus/PJZ110/dodge:16/BP2A.250605.015/U.0:user/release-keys",
             "ro.system.build.date.utc": "1780000000", "ro.build.version.security_patch": "2026-07-01"}


@pytest.fixture
def rom_zip(tmp_path):
    ota, parts = make_ota(tmp_path)
    z = cv.convert(open_rom(ota), tmp_path / "out", chunk=MB)["zip"]
    return z, parts


class Phone:
    def __init__(self, tmp_path, parts, installed=None, props=None, lptools=True, physical_slack=4096):
        self.root = tmp_path / "phone"
        self.block, self.mapper = self.root / "by-name", self.root / "mapper"
        self.block.mkdir(parents=True)
        self.mapper.mkdir()
        for name, data in parts.items():
            if name in LOGICAL:
                (self.mapper / f"{name}_a").write_bytes(b"\xee" * 4096)  # old, smaller
            elif name != "persist":
                for s in ("_a", "_b"):
                    (self.block / f"{name}{s}").write_bytes(b"\xee" * (len(padded(data)) + physical_slack))
        (self.block / "persist").write_bytes(b"P" * 4096)
        values = {"ro.product.device": "dodge", "ro.boot.slot_suffix": "_a"}
        values.update(props or {})
        self.getprop = self.root / "getprop"
        self.getprop.write_text("#!/bin/sh\ncase \"$1\" in\n" + "".join(
            f"  {k}) echo '{v}' ;;\n" for k, v in values.items()) + "esac\n")
        self.getprop.chmod(0o755)
        self.buildprop = self.root / "build.prop"
        inst = dict(INSTALLED)
        inst.update(installed or {})
        self.buildprop.write_text("".join(f"{k}={v}\n" for k, v in inst.items() if v is not None))
        self.log = self.root / "lptools.log"
        self.lptools = self.root / "lptools"
        self.lptools.write_text(f"""#!/bin/sh
echo "$@" >> {self.log}
case "$1" in
  resize) [ -e "{self.mapper}/$2" ] || exit 1; truncate -s "$3" "{self.mapper}/$2" ;;
  create) [ -e "{self.mapper}/$2" ] && exit 1; truncate -s "$3" "{self.mapper}/$2" ;;
  map|unmap) ;;
  *) exit 1 ;;
esac
""")
        self.lptools.chmod(0o755)
        self.has_lptools = lptools

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for d in (self.block, self.mapper) for p in sorted(d.iterdir())}

    def install(self, zip_path, extra_env=None):
        tmp = self.root.parent
        script = tmp / "rom-install.sh"
        with zipfile.ZipFile(zip_path) as z:
            script.write_bytes(z.read("META-INF/com/google/android/update-binary"))
        env = dict(os.environ, OPX_BLOCK_DIR=str(self.block), OPX_MAPPER_DIR=str(self.mapper),
                   OPX_GETPROP=str(self.getprop), OPX_METADATA=str(tmp / "metadata"), OPX_TMP=str(tmp / "rtmp"),
                   OPX_SYSTEM_PROP=str(self.buildprop),
                   OPX_LPTOOLS=str(self.lptools) if self.has_lptools else "no-lptools-here")
        env.update(extra_env or {})
        res = subprocess.run([SH, str(script), "3", "", str(zip_path)], env=env, capture_output=True, text=True)
        return res.returncode, res.stdout + res.stderr


def test_installs_the_rom(rom_zip, tmp_path):
    z, parts = rom_zip
    phone = Phone(tmp_path, parts)
    code, out = phone.install(z)
    assert code == 0, out
    assert "Done: PJZ110" in out and "Same variant: PJZ110" in out and "Not older" in out
    for name, data in parts.items():
        if name == "persist":
            continue
        if name in LOGICAL:
            assert (phone.mapper / f"{name}_a").read_bytes() == padded(data), name
        else:
            got = (phone.block / f"{name}_a").read_bytes()
            assert got[: len(padded(data))] == padded(data), name
            assert (phone.block / f"{name}_b").read_bytes().startswith(b"\xee")  # other slot untouched
    assert (phone.block / "persist").read_bytes() == b"P" * 4096  # never written
    log = phone.log.read_text()
    assert f"resize system_a {len(padded(parts['system']))}" in log
    lines = out.splitlines()
    assert lines.index("  vbmeta") > lines.index("  system")  # AVB last
    assert not (tmp_path / "rtmp" / "opx-rom").exists()  # its work folder is cleaned up


def test_reinstalling_the_same_build_is_fine(rom_zip, tmp_path):
    z, parts = rom_zip
    phone = Phone(tmp_path, parts, installed={"ro.system.build.date.utc": "1790000000"})
    code, out = phone.install(z)
    assert code == 0, out


@pytest.mark.parametrize("change, message", [
    ({"installed": {"ro.system.build.date.utc": "1795000000"}}, "OLDER than the installed Android"),
    ({"installed": {"ro.system.build.fingerprint": "OnePlus/CPH2653/dodge:16/X/U.0:user/release-keys"}},
     "the phone runs CPH2653"),
    ({"props": {"ro.product.device": "kiwi"}}, "this phone is 'kiwi'"),
    ({"props": {"ro.boot.slot_suffix": ""}}, "which slot is active"),
    ({"lptools": False}, "no lptools"),
    ({"physical_slack": -4096}, "doesn't fit this phone's partition"),
])
def test_refuses_and_writes_nothing(rom_zip, tmp_path, change, message):
    z, parts = rom_zip
    phone = Phone(tmp_path, parts, **change)
    before = phone.snapshot()
    code, out = phone.install(z)
    assert code == 1 and message in out and "Nothing was written." in out, out
    assert phone.snapshot() == before
    assert not phone.log.exists()


def test_unreadable_installed_build_is_refused(rom_zip, tmp_path):
    z, parts = rom_zip
    phone = Phone(tmp_path, parts)
    phone.buildprop.unlink()
    before = phone.snapshot()
    code, out = phone.install(z)
    assert code == 1 and "downgrade can't be ruled out" in out
    assert phone.snapshot() == before


def test_merge_in_progress_is_refused(rom_zip, tmp_path):
    z, parts = rom_zip
    phone = Phone(tmp_path, parts)
    snaps = tmp_path / "metadata" / "ota" / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "system_b").write_text("x")
    code, out = phone.install(z)
    assert code == 1 and "merge in progress" in out


def test_damaged_zip_is_refused(rom_zip, tmp_path):
    z, parts = rom_zip
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "images/system.img.001":
                data = data[:-1] + b"\x00"
            dst.writestr(info, data)
    phone = Phone(tmp_path, parts)
    before = phone.snapshot()
    code, out = phone.install(bad)
    assert code == 1 and "system.img.001 in the zip is damaged" in out
    assert phone.snapshot() == before


def test_security_patch_comparison_when_no_dates(tmp_path):
    """A ROM without a build date (image kit): newer patch passes, same patch needs the same build."""
    import romfactory as rf

    fp = "OnePlus/PJZ110/dodge:16/BP2A.250605.015/U.1:user/release-keys"
    src = tmp_path / "kit"
    src.mkdir()
    (src / "boot.img").write_bytes(os.urandom(4096))
    (src / "my_region.img").write_bytes(os.urandom(4096))
    (src / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props={
        "com.android.build.system.fingerprint": fp, "com.android.build.system.security_patch": "2026-08-01"}))
    z = cv.convert(open_rom(src), tmp_path / "out")["zip"]
    parts = {"boot": b"x" * 4096, "my_region": b"x" * 4096, "vbmeta_system": b"x" * 4096}

    for installed, ok in ((("2026-07-01", "other"), True), (("2026-08-01", fp), True),
                          (("2026-08-01", "OnePlus/PJZ110/dodge:16/X/U.0:user/release-keys"), False),
                          (("2026-09-01", "other"), False)):  # a newer, different build is installed
        case = tmp_path / f"case-{installed[0]}-{ok}-{len(installed[1])}"
        case.mkdir()
        fp_installed = installed[1] if "/" in installed[1] else "OnePlus/PJZ110/dodge:16/X/U.0:user/release-keys"
        phone = Phone(case, parts, installed={"ro.system.build.date.utc": None,
                                              "ro.build.version.security_patch": installed[0],
                                              "ro.system.build.fingerprint": fp_installed})
        code, out = phone.install(z)
        assert (code == 0) == ok, (installed, out)
        if ok:  # 'auto' images: boot found as a physical partition, my_region inside super
            assert "resize my_region_a 4096" in phone.log.read_text()
            assert (phone.block / "boot_a").read_bytes().startswith((src / "boot.img").read_bytes())


# --- OPPO's real layout: system carries the chip platform's generic build ------------------

ROM_FP = "OnePlus/PJZ110/dodge:16/BP2A.250605.015/U.1:user/release-keys"
GENERIC = {"ro.system.build.fingerprint": "qti/sun/sun:16/BP2A.250605.015/1:user/release-keys",
           "ro.system.build.date.utc": "1780000000"}


@pytest.mark.parametrize("installed, ok, message", [
    # my_manifest / odm say which phone it is
    ({**GENERIC, "ro.build.fingerprint": "OnePlus/PJZ110/dodge:16/X/U.0:user/release-keys"}, True, None),
    ({**GENERIC, "ro.product.model": "PJZ110"}, True, None),
    # only the chip platform: can't tell the variant
    (GENERIC, False, "Can't confirm the phone runs PJZ110"),
    # another variant anywhere -> no
    ({**GENERIC, "ro.product.model": "PJZ110", "ro.odm.build.fingerprint": "OnePlus/CPH2653/x:16/X/U:user/k"},
     False, "the phone runs CPH2653"),
    # newest installed partition decides the age
    ({**GENERIC, "ro.product.model": "PJZ110", "ro.vendor.build.date.utc": "1795000000"},
     False, "OLDER than the installed Android"),
    # the same build again, even with a partition dated after the ROM's own date
    ({**GENERIC, "ro.build.fingerprint": ROM_FP, "ro.vendor.build.date.utc": "1795000000"}, True, None),
])
def test_oppo_layout(rom_zip, tmp_path, installed, ok, message):
    z, parts = rom_zip
    base = {k: None for k in INSTALLED}
    phone = Phone(tmp_path, parts, installed={**base, **installed})
    before = phone.snapshot()
    code, out = phone.install(z)
    assert (code == 0) == ok, out
    if not ok:
        assert message in out and "Nothing was written." in out
        assert phone.snapshot() == before


def test_chip_platform_board_is_not_the_phone(rom_zip, tmp_path):
    z, parts = rom_zip
    # a different phone with the same chip: only ro.product.board would have matched
    phone = Phone(tmp_path, parts, props={"ro.product.device": "OP5D2BL1", "ro.product.board": "dodge"})
    code, out = phone.install(z)
    assert code == 1 and "this phone is 'OP5D2BL1'" in out
