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
    physical = [e for e in log if e.startswith("flash ") and e[6:].endswith(("_a", "_b"))]
    assert physical[-2:] == ["flash vbmeta_a", "flash vbmeta_b"]  # vbmeta last
    assert "reboot fastboot" in log and log[-1] == "reboot"
    assert fb_state.read()["mode"] == "android"

    journal = json.loads((tmp_path / "journal.json").read_text())
    assert all(s["status"] == "done" for s in journal["steps"])
    assert events[-1]["type"] == "finished"


def test_logical_partitions_need_fastbootd_or_super(adb, fastboot, phone, tmp_path):
    plan, _ = setup(adb, fastboot, tmp_path, fl.FlashOptions(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED]))
    assert any("logical partitions" in p for p in plan.problems)


def test_super_image_flashed_from_bootloader(adb, fastboot, phone, fb_state, tmp_path):
    parts = {k: v for k, v in ROM_PARTS.items() if k not in ("system", "my_region")}
    parts["super"] = b"P" * 8192
    rom = make_rom(tmp_path, parts, folder=True)
    plan, runner = setup(adb, fastboot, tmp_path, fl.FlashOptions(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED]), rom=rom)
    assert plan.ok, plan.problems
    assert [s.kind for s in plan.steps if s.kind.startswith("flash-")] == ["flash-super"]
    runner.run()
    assert (phone / "super.flashed").read_bytes() == b"P" * 8192
    assert "reboot fastboot" not in fb_state.read()["log"]


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
    assert statuses[("flash", "boot")] == "failed"
    assert statuses[("flash", "vbmeta")] == "pending"
    assert fb_state.read()["mode"] == "bootloader"
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
