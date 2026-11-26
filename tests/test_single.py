"""Single-image flashing and reboot buttons, on the simulated phone."""

import pytest
import romfactory as rf
from test_flash import backup_of, phone  # noqa: F401  (fixture)

from opx import device as dev
from opx import flash as fl
from opx import plan as pn
from opx import reboot as rb
from opx import single as sg
from opx.session import open_session

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
    ("abl", b"\x7fELF" + b"\0" * 508, "bootloader chain"),
    ("xbl_config_a", b"\x7fELF" + b"\0" * 508, "bootloader chain"),
], ids=["vbmeta-as-boot", "boot-as-vbmeta", "unknown-dtbo", "too-large", "persist", "userdata", "system", "abl",
        "xbl_config"])
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


def test_boot_chain_names():
    assert all(sg.boot_chain(n) for n in ("abl", "abl_b", "XBL", "xbl_config", "tz_a", "hyp", "aop_config"))
    assert not any(sg.boot_chain(n) for n in ("boot", "init_boot", "vbmeta", "dtbo", "recovery", "ablx"))
    assert sg.boot_chain("modem", extra=["modem"])  # a profile's anti-rollback images count too


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


def test_adb_closed_while_rebooting_is_not_a_failure():
    # real phone: `adb reboot bootloader` printed "error: closed" because the phone was already going
    from opx.tools import Result

    class Closing:
        def bind(self, serial):
            return self

        def run(self, *args, timeout=30):
            return Result(1, "", "error: closed")

    assert "Rebooting" in rb.reboot(phone_in("device"), "recovery", set(), adb=Closing())


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


# --- flashing without a backup (typed NO BACKUP) ----------------------------------


def test_no_backup_flashes_both_slots_from_android(adb, fastboot, phone, fb_state, tmp_path):
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    image = img(tmp_path, "kernel.img", BOOT)
    plan, _ = sg.prepare_image_flash(device, None, "boot", image)
    assert not plan.ok and any("backup" in p.lower() for p in plan.problems)  # still required by default
    plan, warnings = sg.prepare_image_flash(device, None, "boot", image, slot="both", skip_backup=True)
    assert plan.ok, plan.problems
    assert any(w.startswith("No backup") for w in warnings)
    run(plan, s, adb, fastboot, tmp_path)
    for slot in ("a", "b"):
        assert (phone / "by-name" / f"boot_{slot}").read_bytes().startswith(BOOT)


@pytest.mark.parametrize(("partition", "data", "why"), [
    ("abl", b"\x7fELF" + b"\0" * 508, "bootloader chain"),
    ("persist", b"x" * 512, "never flashed"),
    ("system", b"x" * 512, "inside 'super'"),
    ("boot", rf.build_vbmeta(2), "not a boot image"),
])
def test_no_backup_still_refuses_the_dangerous_ones(adb, phone, tmp_path, partition, data, why):
    device = pn.device_from_session(open_session("FAKE0001", adb))
    plan, _ = sg.prepare_image_flash(device, None, partition, img(tmp_path, "x.img", data), skip_backup=True)
    assert not plan.ok and any(why in p for p in plan.problems)


def test_no_backup_still_checks_a_backup_that_is_chosen(adb, phone, tmp_path):
    folder = backup_of(adb, tmp_path)
    device = pn.device_from_session(open_session("FAKE0001", adb))
    device.facts["serial"] = "SOMEONE-ELSE"
    plan, _ = sg.prepare_image_flash(device, folder, "boot", img(tmp_path, "b.img", BOOT), skip_backup=True)
    assert not plan.ok and any("different phone" in p for p in plan.problems)


def test_phone_already_in_fastboot_without_a_backup(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(mode="bootloader")
    device = pn.device_from_fastboot("FAKE0001")
    assert device.source == "fastboot" and device.profile.id == "oplus-generic"
    plan, _ = sg.prepare_image_flash(device, None, "system", img(tmp_path, "s.img", b"x" * 512), skip_backup=True)
    assert not plan.ok  # super partitions are known by name even without the phone's list
    plan, _ = sg.prepare_image_flash(device, None, "boot", img(tmp_path, "b.img", BOOT), skip_backup=True)
    assert plan.ok, plan.problems
    fl.Runner(plan, None, device.profile, adb, fastboot, tmp_path / "j.json", tmp_path / "w", wait_timeout=5).run()
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(BOOT)


def test_cli_flash_image_no_backup(ready, capsys, monkeypatch, tmp_path):
    phone, state, _, _ = ready
    image = img(tmp_path, "kernel.img", BOOT)
    args = ["flash-image", "boot", str(image), "--no-backup", "--work", str(tmp_path / "w")]
    answers(monkeypatch, "FLASH")  # the usual word isn't enough without a backup
    assert cli.main(args) == 1 and state.read()["log"] == []
    answers(monkeypatch, "NO BACKUP")
    assert cli.main(args) == 0
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(BOOT)


def test_no_backup_on_a_phone_without_root(adb, fastboot, phone, fb_state, tmp_path, monkeypatch):
    """The case it's for: no root, so no backup and no partition list. Super partitions are still
    refused by name, and a recovery / boot image can be flashed."""
    monkeypatch.setenv("FAKE_ACCESS", "none")
    s = open_session("FAKE0001", adb)
    assert not s.access.usable
    device = pn.device_for_session(s)
    plan, _ = sg.prepare_image_flash(device, None, "system", img(tmp_path, "s.img", b"x" * 512), skip_backup=True)
    assert not plan.ok and any("inside 'super'" in p for p in plan.problems)
    plan, _ = sg.prepare_image_flash(device, None, "abl", img(tmp_path, "a.img", b"\x7fELF" + b"\0" * 508),
                                     skip_backup=True)
    assert not plan.ok
    plan, _ = sg.prepare_image_flash(device, None, "boot", img(tmp_path, "b.img", BOOT), skip_backup=True)
    assert plan.ok, plan.problems
    fl.Runner(plan, None, device.profile, adb, fastboot, tmp_path / "j.json", tmp_path / "w", wait_timeout=5).run()
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(BOOT)
