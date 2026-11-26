import json
import threading

import pytest
import romfactory as rf
from test_plan import put, sized

from opx import backup as bk
from opx import flash as fl
from opx import plan as pn
from opx.fastboot import Fastboot, RefusedPartition
from opx.rom.ota import open_rom
from opx.session import open_session

ROM_PARTS = {
    "vbmeta": rf.build_vbmeta(2),
    "xbl_config": rf.build_qcom_elf(arb=1),
    "dtbo": b"D" * 4096,
    "boot": b"B" * 8192,
    "oplusstanvbk": b"O" * 4096,
    "system": b"S" * 8192,
    "my_region": b"R" * 4096,
}


@pytest.fixture
def phone(fake_device, fb_state):
    for slot in ("_a", "_b"):
        put(fake_device, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(fake_device, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(fake_device, f"dtbo{slot}", b"\0" * 65536)
    return fake_device


def make_rom(tmp_path, parts=None, folder=False):
    parts = parts or ROM_PARTS
    if folder:
        d = tmp_path / "romdir"
        d.mkdir()
        for name, data in parts.items():
            (d / f"{name}.img").write_bytes(data)
        (d / "META-INF" / "com" / "android").mkdir(parents=True)
        (d / "META-INF" / "com" / "android" / "metadata").write_text(
            "".join(f"{k}={v}\n" for k, v in rf.ota_metadata().items())
        )
        return open_rom(d)
    payload = rf.build_payload(parts, groups={"main": ["system", "my_region"]})
    return open_rom(rf.build_ota_zip(tmp_path / "rom.zip", payload, rf.ota_metadata()))


def backup_of(adb, tmp_path, mode="critical"):
    s = open_session("FAKE0001", adb)
    return bk.run_backup(s.shell, bk.build_plan(s.shell, s.profile, mode, s.props), tmp_path / "backups")


def setup(adb, fastboot, tmp_path, options=None, rom=None, backup="critical"):
    rom = rom or make_rom(tmp_path)
    folder = backup_of(adb, tmp_path, backup) if backup else None
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    report = pn.build_report(rom, device)
    plan = fl.prepare(rom, report, device, options or fl.FlashOptions(), folder)
    runner = fl.Runner(plan, rom, s.profile, adb, fastboot, tmp_path / "journal.json", tmp_path / "work", wait_timeout=5)
    return plan, runner


ALLOW = dict(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED], allow_fastbootd=True)


def test_blocked_by_default_and_nothing_written(adb, fastboot, phone, fb_state, tmp_path):
    plan, runner = setup(adb, fastboot, tmp_path)
    assert not plan.ok
    assert any("Qualcomm ARB" in p for p in plan.problems)
    with pytest.raises(fl.FlashError, match="blocked"):
        runner.run()
    assert fb_state.read()["log"] == [] and fb_state.read()["mode"] == "android"


def test_backup_is_required(adb, fastboot, phone, tmp_path):
    plan, _ = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW), backup=None)
    assert any("backup" in p.lower() for p in plan.problems)


def test_network_backup_is_not_enough(adb, fastboot, phone, tmp_path):
    plan, _ = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW), backup="network")
    assert any("Critical or Full" in p for p in plan.problems)


def test_full_flash_via_fastbootd(adb, fastboot, phone, fb_state, tmp_path):
    persist_before = (phone / "by-name" / "persist").read_bytes()
    stanvbk_before = (phone / "by-name" / "oplusstanvbk_a").read_bytes()
    region_before = (phone / "mapper" / "my_region_a").read_bytes()
    events = []
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    assert plan.ok, plan.problems
    runner.emit = events.append
    runner.run()

    for slot in ("_a", "_b"):
        assert (phone / "by-name" / f"dtbo{slot}").read_bytes().startswith(b"D" * 4096)
        assert (phone / "by-name" / f"boot{slot}").read_bytes().startswith(b"B" * 8192)
    assert (phone / "mapper" / "system_a").read_bytes() == rf.pad(b"S" * 8192)
    # protected and optional partitions untouched (my_region keeps the phone's own region file)
    assert (phone / "mapper" / "my_region_a").read_bytes() == region_before
    assert (phone / "by-name" / "persist").read_bytes() == persist_before
    assert (phone / "by-name" / "oplusstanvbk_a").read_bytes() == stanvbk_before

    log = fb_state.read()["log"]
    assert log[0] == "adb reboot bootloader"
    in_bootloader = log[:log.index("reboot fastboot")]
    physical = [e for e in in_bootloader if e.startswith("flash ") and e[6:].endswith(("_a", "_b"))]
    assert physical[-2:] == ["flash vbmeta_a", "flash vbmeta_b"]  # vbmeta last in the bootloader
    # OnePlus 13, as Super Flasher: xbl_config (firmware) from fastbootd
    assert log.index("reboot fastboot") < log.index("flash xbl_config_a")
    assert "reboot fastboot" in log and log[-1] == "reboot"
    assert fb_state.read()["mode"] == "android"

    journal = json.loads((tmp_path / "journal.json").read_text())
    assert all(s["status"] == "done" for s in journal["steps"])
    assert events[-1]["type"] == "finished"


def test_logical_partitions_need_fastbootd_or_super(adb, fastboot, phone, tmp_path):
    import dataclasses

    plan, _ = setup(adb, fastboot, tmp_path, fl.FlashOptions(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED]))
    assert plan.ok, plan.problems  # a OnePlus 13 always uses fastbootd: nothing to allow
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    device = dataclasses.replace(device, profile=dataclasses.replace(device.profile, firmware_in_fastbootd=False))
    rom = make_rom(tmp_path)
    plan = fl.prepare(rom, pn.build_report(rom, device), device,
                      fl.FlashOptions(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED]), backup_of(adb, tmp_path))
    assert any("logical partitions" in p for p in plan.problems)  # other phones: fastbootd must be allowed


def test_super_image_flashed_from_bootloader(adb, fastboot, phone, fb_state, tmp_path):
    parts = {k: v for k, v in ROM_PARTS.items() if k not in ("system", "my_region")}
    parts["super"] = b"P" * 8192
    rom = make_rom(tmp_path, parts, folder=True)
    # super.img from the bootloader; a OnePlus 13 then writes its firmware from fastbootd
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW), rom=rom)
    assert plan.ok, plan.problems
    kinds = [s.kind for s in plan.steps]
    assert "flash-logical" not in kinds
    assert kinds.index("flash-super") < kinds.index("flash-any") < kinds.index("to-fastbootd")
    runner.run()
    assert (phone / "super.flashed").read_bytes() == b"P" * 8192
    log = fb_state.read()["log"]
    assert log.index("flash super") < log.index("reboot fastboot")


def test_optional_partition_only_when_chosen(adb, fastboot, phone, fb_state, tmp_path):
    options = fl.FlashOptions(include_optional=["oplusstanvbk", "my_region"], **ALLOW)
    plan, runner = setup(adb, fastboot, tmp_path, options)
    assert ("flash-logical", "my_region") in [(s.kind, s.partition) for s in plan.steps]
    runner.run()
    assert (phone / "by-name" / "oplusstanvbk_b").read_bytes().startswith(b"O" * 4096)
    assert (phone / "mapper" / "my_region_a").read_bytes() == rf.pad(b"R" * 4096)


def test_failure_stops_and_resume_finishes(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(fail_flash="boot_b")
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    with pytest.raises(fl.FlashError, match="Do not reboot"):
        runner.run()
    journal = json.loads((tmp_path / "journal.json").read_text())
    statuses = {(s["kind"], s["partition"]): s["status"] for s in journal["steps"]}
    # a OnePlus 13: boot failed in the bootloader, so it was retried from fastbootd, where it failed too
    assert statuses[("flash-any", "boot")] == "failed"
    assert statuses[("flash-any", "vbmeta")] == "done"
    assert fb_state.read()["mode"] == "fastbootd"
    flashes_before = fb_state.read()["log"].count("flash dtbo_a")

    fb_state(fail_flash=None)
    from opx.profile import load

    resumed = fl.resume_runner(tmp_path / "journal.json", load("oneplus-13"), adb, fastboot, wait_timeout=5)
    resumed.run()
    log = fb_state.read()["log"]
    assert log.count("flash dtbo_a") == flashes_before  # finished steps are not repeated
    assert "flash boot_b" in log and log[-1] == "reboot"


def test_wipe_option(adb, fastboot, phone, fb_state, tmp_path):
    _, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(wipe_data=True, **ALLOW))
    runner.run()
    assert fb_state.read()["wiped"] is True


def test_locked_bootloader_writes_nothing(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(unlocked=False)
    _, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    with pytest.raises(fl.FlashError, match="locked"):
        runner.run()
    assert not [e for e in fb_state.read()["log"] if e.startswith("flash")]


def test_cos17_cn_refuses_to_reboot(adb, fastboot, phone, fb_state, tmp_path):
    props = json.loads((phone / "props.json").read_text())
    props["ro.build.display.id"] = "PJZ110_17.0.0.120(CN01)"
    (phone / "props.json").write_text(json.dumps(props))
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    assert any("known to break the bootloader" in p for p in plan.problems)
    with pytest.raises(fl.FlashError):
        runner.run()
    assert fb_state.read()["mode"] == "android" and fb_state.read()["log"] == []


def test_unknown_broken_fastbootd_times_out_cleanly(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(fastbootd_broken=True)
    _, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    runner.wait_timeout = 0.3
    with pytest.raises(fl.FlashError, match="fastbootd"):
        runner.run()
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert next(s for s in journal["steps"] if s["kind"] == "to-fastbootd")["status"] == "failed"


def test_backup_from_another_phone_is_refused(adb, fastboot, phone, tmp_path):
    folder = backup_of(adb, tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["device"]["serial"] = "OTHER123"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    problems = fl.check_backup(folder, {"serial": "FAKE0001", "model": "PJZ110"})
    assert any("different phone" in p for p in problems)


def test_cancel_before_writing(adb, fastboot, phone, fb_state, tmp_path):
    _, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    runner.cancel = threading.Event()
    runner.cancel.set()
    with pytest.raises(fl.Cancelled):
        runner.run()
    assert fb_state.read()["log"] == []


def test_never_flash_refused_at_write_time(fastboot, fb_state, tmp_path):
    from opx.profile import load

    fb_state(mode="bootloader")
    img = tmp_path / "x.img"
    img.write_bytes(b"x" * 512)
    fb = Fastboot(fastboot.bind("FAKE0001"), load("oneplus-13"))
    for name in ("persist", "modemst1", "ocdt", "userdata", "oplusreserve2"):
        with pytest.raises(RefusedPartition):
            fb.flash(name, img)
    assert fb_state.read()["log"] == []


def test_personal_data_partitions_never_flashed_whatever_the_profile(fastboot, fb_state, tmp_path):
    import dataclasses

    from opx.profile import load

    fb_state(mode="bootloader")
    img = tmp_path / "x.img"
    img.write_bytes(b"x" * 512)
    careless = dataclasses.replace(load("oneplus-13"), never_flash=[])
    fb = Fastboot(fastboot.bind("FAKE0001"), careless)
    for name in ("userdata", "metadata", "frp"):
        with pytest.raises(RefusedPartition):
            fb.flash(name, img)
    assert fb_state.read()["log"] == []


def test_keep_custom_recovery(adb, fastboot, phone, fb_state, tmp_path):
    """The ROM has a recovery; with keep_recovery the phone's own (AERA / OrangeFox) isn't written."""
    for slot in ("_a", "_b"):
        put(phone, f"recovery{slot}", b"\xaa" * 65536)
    rom = make_rom(tmp_path, {**ROM_PARTS, "recovery": b"C" * 8192})
    plan, _ = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW), rom=rom)
    assert any(s.partition == "recovery" for s in plan.steps)
    plan, runner = setup(adb, fastboot, tmp_path / "k", fl.FlashOptions(keep_recovery=True, **ALLOW), rom=rom)
    assert plan.ok, plan.problems
    assert not any(s.partition == "recovery" for s in plan.steps)
    runner.run()
    assert (phone / "by-name" / "recovery_a").read_bytes() == b"\xaa" * 65536
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(b"B" * 8192)


def test_no_backup_flash_on_a_phone_without_root(adb, fastboot, phone, fb_state, tmp_path, monkeypatch):
    # backup recommended, not forced: typed NO BACKUP, unrooted phone compared from what Android reports
    monkeypatch.setenv("FAKE_ACCESS", "none")
    stanvbk_before = (phone / "by-name" / "oplusstanvbk_a").read_bytes()
    rom = make_rom(tmp_path)
    s = open_session("FAKE0001", adb)
    device = pn.device_for_session(s)
    assert device.source == "props"
    report = pn.build_report(rom, device)
    plan = fl.prepare(rom, report, device, fl.FlashOptions(**ALLOW), None)
    assert any("backup" in p.lower() for p in plan.problems)  # without NO BACKUP: still required
    plan = fl.prepare(rom, report, device, fl.FlashOptions(**ALLOW, no_backup=True), None)
    assert plan.ok, plan.problems
    assert plan.warnings == [fl.NO_BACKUP_ROM_WARNING]
    fl.Runner(plan, rom, s.profile, adb, fastboot, tmp_path / "journal.json", tmp_path / "work", wait_timeout=5).run()
    for slot in ("_a", "_b"):
        assert (phone / "by-name" / f"dtbo{slot}").read_bytes().startswith(b"D" * 4096)
    assert (phone / "by-name" / "oplusstanvbk_a").read_bytes() == stanvbk_before  # the phone keeps its own


def test_no_backup_keeps_every_other_gate(adb, fastboot, phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "none")
    s = open_session("FAKE0001", adb)
    device = pn.device_for_session(s)
    rom = make_rom(tmp_path)
    report = pn.build_report(rom, device)
    # the unread Qualcomm ARB still needs ARB CHECKED
    plan = fl.prepare(rom, report, device, fl.FlashOptions(allow_fastbootd=True, no_backup=True), None)
    assert any("Qualcomm ARB" in p for p in plan.problems)
    # a ROM for another phone is still refused
    payload = rf.build_payload(ROM_PARTS, groups={"main": ["system", "my_region"]})
    other = open_rom(rf.build_ota_zip(tmp_path / "other.zip", payload, rf.ota_metadata(codename="otherphone")))
    plan = fl.prepare(other, pn.build_report(other, device), device, fl.FlashOptions(**ALLOW, no_backup=True), None)
    assert any("Device match" in p for p in plan.problems)


def test_no_backup_with_a_network_backup_for_own_network_files(adb, fastboot, phone, tmp_path):
    # NO BACKUP, but the Network essentials backup is chosen: accepted, for the phone's own network files only
    s = open_session("FAKE0001", adb)
    folder = backup_of(adb, tmp_path, mode="network")
    rom = make_rom(tmp_path)
    device = pn.device_from_session(s)
    report = pn.build_report(rom, device)
    opts = fl.FlashOptions(**ALLOW, restore_from_backup=["oplusstanvbk"])
    plan = fl.prepare(rom, report, device, opts, folder)
    assert any("Critical or Full" in p for p in plan.problems)  # as a full backup it isn't enough
    opts.no_backup = True
    plan = fl.prepare(rom, report, device, opts, folder)
    assert plan.ok, plan.problems
    assert any(st.partition == "oplusstanvbk" and st.source == "backup" for st in plan.steps)


def test_firmware_from_fastbootd_when_the_bootloader_refuses_it(adb, fastboot, phone, fb_state, tmp_path):
    # the real OnePlus 13: its bootloader writes boot, dtbo, modem, vbmeta ... (the profile's
    # bootloader_images, as Super Flasher does) and refuses the rest; its fastbootd refuses modem.
    fb_state(refuse={"bootloader": ["xbl_config", "oplusstanvbk"], "fastbootd": ["modem", "vbmeta"]})
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    assert plan.ok, plan.problems
    by_name = {st.partition: st.kind for st in plan.steps if st.kind.startswith("flash-") and st.partition}
    assert by_name["vbmeta"] == "flash-any" and by_name["dtbo"] == "flash-any"
    assert by_name["xbl_config"] == "flash-fw"
    kinds = [st.kind for st in plan.steps]
    assert kinds.index("to-fastbootd") < kinds.index("flash-fw") < kinds.index("free-super") < kinds.index("flash-logical")
    runner.run()
    log = fb_state.read()["log"]
    fbd = log.index("reboot fastboot")
    assert log.index("flash vbmeta_a") < fbd < log.index("flash xbl_config_a")
    assert (phone / "by-name" / "xbl_config_b").read_bytes()[:4] == b"\x7fELF"
    # a bootloader-list image the bootloader refuses after all: written from fastbootd (fallback)
    fb_state(refuse={"bootloader": ["dtbo"]}, log=[])
    (tmp_path / "again").mkdir()
    plan, runner = setup(adb, fastboot, tmp_path / "again", fl.FlashOptions(**ALLOW))
    runner.run()
    journal = json.loads((tmp_path / "again" / "journal.json").read_text())
    dtbo = next(s for s in journal["steps"] if s["partition"] == "dtbo")
    assert dtbo["status"] == "done" and "from fastbootd" in dtbo["detail"]


def test_other_slots_logical_copies_are_removed_first(adb, fastboot, phone, fb_state, tmp_path):
    # super has room for one set of system partitions: the other slot's and any OTA -cow go first
    (phone / "mapper" / "system_b").write_bytes(b"x" * 4096)
    (phone / "mapper" / "system_a-cow").write_bytes(b"c" * 4096)
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    runner.run()
    log = fb_state.read()["log"]
    assert "delete-logical-partition system_b" in log and "delete-logical-partition system_a-cow" in log
    assert log.index("delete-logical-partition system_b") < log.index("flash system")
    assert (phone / "mapper" / "system_a").exists() and not (phone / "mapper" / "system_b").exists()


def test_refused_in_both_modes_stops_with_the_phones_reason(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(refuse={"bootloader": ["dtbo"], "fastbootd": ["dtbo"]})
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    with pytest.raises(fl.FlashError, match="Flashing is not allowed for dtbo_a"):
        runner.run()


def test_resume_after_a_deferred_image(adb, fastboot, phone, fb_state, tmp_path):
    # stopped in fastbootd after the bootloader refused dtbo: the resume still writes it
    fb_state(refuse={"bootloader": ["dtbo"]}, fail_flash="system")
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    with pytest.raises(fl.FlashError):
        runner.run()
    fb_state(fail_flash=None)
    from opx.profile import load

    fl.resume_runner(tmp_path / "journal.json", load("oneplus-13"), adb, fastboot, wait_timeout=5).run()
    assert (phone / "by-name" / "dtbo_b").read_bytes().startswith(b"D" * 4096)


def test_phone_dropping_off_usb_stops_and_resumes(adb, fastboot, phone, fb_state, tmp_path):
    # real OnePlus 13 on a flaky USB port: "Sending 'boot' FAILED (Status read failed (Too many links))",
    # then the phone restarts to Android. That's not the bootloader refusing boot: stop, don't defer and
    # carry on (the next fastboot command would wait for the phone for half an hour).
    fb_state(drop_on_flash="boot_a")
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    with pytest.raises(fl.FlashError, match="dropped off USB") as err:
        runner.run()
    assert "Resume" in str(err.value)
    journal = json.loads((tmp_path / "journal.json").read_text())
    boot = next(s for s in journal["steps"] if s["partition"] == "boot")
    assert boot["status"] == "failed"
    assert not any(s["status"] == "deferred" for s in journal["steps"])
    assert not any(c.startswith("flash init_boot") for c in fb_state.read().get("log", []))
    # Resume: the phone is back in Android; OPxFlasher puts it in the bootloader and carries on
    fb_state(drop_on_flash=None)
    from opx.profile import load

    fl.resume_runner(tmp_path / "journal.json", load("oneplus-13"), adb, fastboot, wait_timeout=5).run()
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert all(s["status"] == "done" for s in journal["steps"]), [s for s in journal["steps"] if s["status"] != "done"]


def test_phone_gone_before_a_step_is_not_waited_for(adb, fastboot, phone, fb_state, tmp_path):
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    from opx.fastboot import LostPhone
    from opx.profile import load

    runner.fb = Fastboot(fastboot.bind("FAKE0001"), load("oneplus-13"))
    fb_state(mode="android")
    with pytest.raises(LostPhone, match="no longer in fastboot"):
        runner._ensure("bootloader")
    assert runner.fb is None


def test_transport_errors_are_told_apart_from_refusals():
    from opx.fastboot import is_transport_error

    assert is_transport_error("Sending 'boot' (98304 KB)  FAILED (Status read failed (Too many links))")
    assert is_transport_error("FAILED (Write to device failed (No such device))")
    assert not is_transport_error("Writing 'xbl_a' FAILED (remote: 'Flashing is not allowed for xbl_a')")


def test_other_phones_still_write_firmware_from_the_bootloader(adb, fastboot, phone, fb_state, tmp_path):
    import dataclasses

    rom = make_rom(tmp_path)
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    device = dataclasses.replace(device, profile=dataclasses.replace(device.profile, firmware_in_fastbootd=False))
    plan = fl.prepare(rom, pn.build_report(rom, device), device, fl.FlashOptions(**ALLOW), backup_of(adb, tmp_path))
    assert plan.ok, plan.problems
    kinds = [st.kind for st in plan.steps]
    assert "flash-fw" not in kinds and kinds.index("flash") < kinds.index("to-fastbootd")
    fl.Runner(plan, rom, device.profile, adb, fastboot, tmp_path / "journal.json", tmp_path / "work",
              wait_timeout=5).run()
    log = fb_state.read()["log"]
    assert next(i for i, c in enumerate(log) if c.startswith("flash dtbo")) < log.index("reboot fastboot")


def test_network_files_from_their_own_backup(adb, fastboot, phone, tmp_path):
    # the main backup is skipped (NO BACKUP), the network files come from the Network essentials folder
    s = open_session("FAKE0001", adb)
    net = backup_of(adb, tmp_path, mode="network")
    rom = make_rom(tmp_path)
    device = pn.device_from_session(s)
    report = pn.build_report(rom, device)
    opts = fl.FlashOptions(**ALLOW, no_backup=True, restore_from_backup=["oplusstanvbk"], network_backup=str(net))
    plan = fl.prepare(rom, report, device, opts, None)
    assert plan.ok, plan.problems
    assert any(st.partition == "oplusstanvbk" and st.source == "backup" for st in plan.steps)
    files = fl.verified_backup_files(plan, lambda e: None)
    assert any(k.startswith("oplusstanvbk") for k in files)


def test_reboot_to_fastbootd_that_hangs_the_fastboot_tool(adb, fastboot, phone, fb_state, tmp_path, monkeypatch):
    # Windows, real OnePlus 13: `fastboot reboot fastboot` reboots the phone but the tool itself hangs
    # waiting for it, and is killed after the timeout. The flash must go on once fastbootd shows up.
    from opx import tools

    real_run = tools.Tool.run

    def run(self, *args, timeout=30):
        res = real_run(self, *args, timeout=timeout)
        if args[:2] == ("reboot", "fastboot"):
            return tools.Result(124, "", "fastboot timed out after 30s")
        return res

    monkeypatch.setattr(tools.Tool, "run", run)
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    assert plan.ok, plan.problems
    runner.run()
    assert (phone / "by-name" / "dtbo_b").read_bytes().startswith(b"D" * 4096)


def test_resume_when_the_phone_is_already_in_fastbootd(adb, fastboot, phone, fb_state, tmp_path):
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    fb_state(fail_flash="boot_a")
    with pytest.raises(fl.FlashError):
        runner.run()
    assert fb_state.read()["mode"] == "fastbootd"
    # mark the reboot to fastbootd as not done, as when the tool timed out on it
    journal = json.loads((tmp_path / "journal.json").read_text())
    for st in journal["steps"]:
        if st["kind"] == "to-fastbootd":
            st["status"] = "failed"
    (tmp_path / "journal.json").write_text(json.dumps(journal))
    fb_state(fail_flash=None)
    from opx.profile import load

    fl.resume_runner(tmp_path / "journal.json", load("oneplus-13"), adb, fastboot, wait_timeout=5).run()
    assert (phone / "by-name" / "dtbo_b").read_bytes().startswith(b"D" * 4096)


def test_partition_size_not_answered_is_left_to_fastboot(adb, fastboot, phone, fb_state, tmp_path):
    # real OnePlus 13 fastbootd: no partition-size for modem_a / modem_b, though they exist
    fb_state(no_size=["dtbo_a", "dtbo_b"])
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW))
    runner.run()
    assert (phone / "by-name" / "dtbo_b").read_bytes().startswith(b"D" * 4096)
    journal = json.loads((tmp_path / "journal.json").read_text())
    dtbo = next(s for s in journal["steps"] if s["partition"] == "dtbo")
    assert "size checked by fastboot" in dtbo["detail"]


def test_real_oneplus_13_behaviour_end_to_end(adb, fastboot, phone, fb_state, tmp_path, monkeypatch):
    # Everything the real OnePlus 13 did on 26 Nov 2026, at once:
    # - the bootloader writes only boot, dtbo, init_boot, modem, recovery, vbmeta*, vendor_boot
    # - fastbootd refuses modem and doesn't report its size
    # - `fastboot reboot fastboot` hangs the tool after rebooting the phone
    # - the other slot has its own copy of system in super
    from opx import tools

    for slot in ("_a", "_b"):
        put(phone, f"modem{slot}", b"\0" * 65536)
        put(phone, f"abl{slot}", b"\0" * 65536)
    (phone / "mapper" / "system_b").write_bytes(b"x" * 4096)
    fb_state(refuse={"bootloader": ["xbl_config", "abl", "oplusstanvbk"], "fastbootd": ["modem"]},
             no_size=["modem_a", "modem_b"])
    real_run = tools.Tool.run

    def run(self, *args, timeout=30):
        res = real_run(self, *args, timeout=timeout)
        return tools.Result(124, "", "fastboot timed out") if args[:2] == ("reboot", "fastboot") else res

    monkeypatch.setattr(tools.Tool, "run", run)
    parts = {**ROM_PARTS, "modem": b"M" * 4096, "abl": rf.build_qcom_elf(arb=1)}
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(**ALLOW), rom=make_rom(tmp_path, parts))
    assert plan.ok, plan.problems
    runner.run()
    for name, data in (("modem", b"M" * 4096), ("dtbo", b"D" * 4096), ("boot", b"B" * 8192)):
        for slot in ("_a", "_b"):
            assert (phone / "by-name" / f"{name}{slot}").read_bytes().startswith(data), name + slot
    for slot in ("_a", "_b"):
        assert (phone / "by-name" / f"abl{slot}").read_bytes()[:4] == b"\x7fELF"
    log = fb_state.read()["log"]
    fbd = log.index("reboot fastboot")
    assert log.index("flash modem_a") < fbd < log.index("flash abl_a") < log.index("flash system")
    assert log[-1] == "reboot" and "delete-logical-partition system_b" in log
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert all(s["status"] == "done" for s in journal["steps"]), [s for s in journal["steps"] if s["status"] != "done"]
