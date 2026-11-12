"""Single-image flashing and reboot buttons, on the simulated phone."""

import pytest
import romfactory as rf

from opx import device as dev
from opx import flash as fl
from opx import plan as pn
from opx import reboot as rb
from opx import single as sg
from opx.session import open_session
from test_flash import backup_of, phone  # noqa: F401  (fixture)

BOOT = rf.build_boot_image(b"my-custom-kernel", header_version=4)


def img(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def go(adb, fastboot, tmp_path, partition, data, slot="current", **kw):
    folder = backup_of(adb, tmp_path)
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    plan, warnings = sg.prepare_image_flash(device, folder, partition, img(tmp_path, "x.img", data), slot, **kw)
    return s, plan, warnings


def run(plan, s, adb, fastboot, tmp_path):
    return fl.Runner(plan, None, s.profile, adb, fastboot, tmp_path / "j.json", tmp_path / "w", wait_timeout=5).run()


def test_boot_to_current_slot(adb, fastboot, phone, fb_state, tmp_path):
    s, plan, warnings = go(adb, fastboot, tmp_path, "boot", BOOT)
    assert plan.ok, plan.problems
    assert any("exact build" in w for w in warnings)
    fb_state(slot="b")
    run(plan, s, adb, fastboot, tmp_path)
    assert (phone / "by-name" / "boot_b").read_bytes().startswith(BOOT)
    assert not (phone / "by-name" / "boot_a").read_bytes().startswith(BOOT)
    assert "flash boot_b" in fb_state.read()["log"]


def test_both_slots(adb, fastboot, phone, fb_state, tmp_path):
    s, plan, _ = go(adb, fastboot, tmp_path, "boot", BOOT, slot="both")
    run(plan, s, adb, fastboot, tmp_path)
    for slot in ("a", "b"):
        assert (phone / "by-name" / f"boot_{slot}").read_bytes().startswith(BOOT)


def test_vbmeta_with_verity_disabled(adb, fastboot, phone, fb_state, tmp_path):
    vb = rf.build_vbmeta(2)
    s, plan, warnings = go(adb, fastboot, tmp_path, "vbmeta", vb, disable_verity=True)
    assert plan.ok, plan.problems
    assert any("Verity" in w for w in warnings)
    run(plan, s, adb, fastboot, tmp_path)
    assert "--disable-verity --disable-verification flash vbmeta_a" in fb_state.read()["log"]


@pytest.mark.parametrize("partition,data,why", [
    ("boot", rf.build_vbmeta(2), "vbmeta image, not a boot"),
    ("vbmeta", BOOT, "boot image, not a vbmeta"),
    ("dtbo", b"\0" * 4096, "not a known image type"),
    ("boot", BOOT + b"\0" * 70000, "larger than boot"),
    ("persist", b"x" * 512, "never flashed"),
    ("userdata", b"x" * 512, "never flashed"),
    ("system", b"x" * 512, "inside 'super'"),
])
def test_refused_and_nothing_written(adb, fastboot, phone, fb_state, tmp_path, partition, data, why):
    s, plan, _ = go(adb, fastboot, tmp_path, partition, data)
    assert not plan.ok and any(why in p for p in plan.problems), plan.problems
    with pytest.raises(fl.FlashError):
        run(plan, s, adb, fastboot, tmp_path)
    assert not any(e.startswith("flash") for e in fb_state.read().get("log", []))


def test_verity_flag_only_for_vbmeta(adb, fastboot, phone, tmp_path):
    _, plan, _ = go(adb, fastboot, tmp_path, "boot", BOOT, disable_verity=True)
    assert any("only applies to vbmeta" in p for p in plan.problems)


def test_needs_a_backup(adb, phone, tmp_path):
    device = pn.device_from_session(open_session("FAKE0001", adb))
    plan, _ = sg.prepare_image_flash(device, None, "boot", img(tmp_path, "b.img", BOOT))
    assert any("backup" in p for p in plan.problems)


def test_image_changed_after_check_is_not_written(adb, fastboot, phone, fb_state, tmp_path):
    s, plan, _ = go(adb, fastboot, tmp_path, "boot", BOOT)
    (tmp_path / "x.img").write_bytes(BOOT.replace(b"my-custom", b"evil-swap"))
    with pytest.raises(fl.FlashError, match="changed"):
        run(plan, s, adb, fastboot, tmp_path)


def test_unknown_image_type_is_a_warning(tmp_path):
    _, warnings = sg.check_image("xbl_config", img(tmp_path, "x.img", b"\x7fELF...."))
    assert any("can't check" in w for w in warnings)


# --- reboots ---------------------------------------------------------------

def phone_in(scan_state):
    transport = "fastboot" if scan_state in ("bootloader", "fastbootd") else "adb"
    return dev.Device(serial="FAKE0001", transport=transport, state=scan_state)


def test_reboot_choices_from_android():
    got = {c.target: c for c in rb.choices(phone_in("device"), set())}
    assert not got["system"].allowed and got["bootloader"].allowed and got["fastbootd"].allowed
    assert not got["fastbootd"].confirm


def test_reboot_refused_on_known_bad_firmware():
    got = {c.target: c for c in rb.choices(phone_in("device"), {"fastbootd", "bootloader"})}
    assert not got["fastbootd"].allowed and "brick" in got["fastbootd"].reason
    assert not got["bootloader"].allowed
    assert got["recovery"].allowed and got["system"].allowed is False


def test_fastbootd_from_fastboot_needs_confirmation():
    got = {c.target: c for c in rb.choices(phone_in("bootloader"), None)}
    assert got["fastbootd"].allowed and got["fastbootd"].confirm and "ColorOS 17" in got["fastbootd"].reason
    assert not got["bootloader"].allowed and got["recovery"].allowed


def test_no_phone_no_reboots():
    assert not any(c.allowed for c in rb.choices(None, set()))
    assert not any(c.allowed for c in rb.choices(phone_in("unauthorized"), set()))


def test_reboot_commands(adb, fastboot, phone, fb_state):
    rb.reboot(phone_in("device"), "recovery", set(), adb=adb)
    assert fb_state.read()["mode"] == "recovery"
    fb_state(mode="bootloader")
    rb.reboot(phone_in("bootloader"), "fastbootd", set(), fastboot=fastboot)
    assert fb_state.read()["mode"] == "fastbootd"
    rb.reboot(phone_in("fastbootd"), "system", set(), fastboot=fastboot)
    assert fb_state.read()["mode"] == "android"
    with pytest.raises(rb.RebootError, match="brick"):
        rb.reboot(phone_in("device"), "fastbootd", {"fastbootd"}, adb=adb)


# --- command line ------------------------------------------------------------

from test_flash_cli import answers, ready  # noqa: E402,F401  (fixture)

from opx import cli  # noqa: E402


def test_cli_flash_image(ready, capsys, monkeypatch, tmp_path):
    phone, state, _, backup = ready
    image = img(tmp_path, "boot.img", BOOT)
    args = ["flash-image", "boot", str(image), "--backup", str(backup), "--work", str(tmp_path / "w")]
    assert cli.main(args + ["--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out and state.read()["log"] == []

    answers(monkeypatch, "no")
    assert cli.main(args) == 1 and state.read()["log"] == []

    answers(monkeypatch, "FLASH")
    assert cli.main(args) == 0
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(BOOT)


def test_cli_flash_image_refuses_wrong_type(ready, capsys, tmp_path):
    _, state, _, backup = ready
    image = img(tmp_path, "vb.img", rf.build_vbmeta(2))
    assert cli.main(["flash-image", "boot", str(image), "--backup", str(backup)]) == 3
    assert "not a boot image" in capsys.readouterr().out
    assert state.read()["log"] == []


def test_cli_reboot(ready, capsys):
    _, state, _, _ = ready
    assert cli.main(["reboot", "bootloader"]) == 0
    assert state.read()["mode"] == "bootloader"
    assert cli.main(["reboot", "bootloader"]) == 3  # already there
    assert cli.main(["reboot", "system"]) == 0
    assert state.read()["mode"] == "android"
