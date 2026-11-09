import json
import os
import time

import pytest

import romfactory as rf
from opx import backup as bk
from opx import flash as fl
from opx import plan as pn
from opx import root as rt
from opx.rom.ota import open_rom
from opx.session import open_session
from test_flash import backup_of, phone  # noqa: F401  (fixture)

STOCK = rf.build_boot_image(b"stock-ramdisk")
FINGERPRINT = "OnePlus/PJZ110/testdevice:16/BP2A.250605.015/U.1:user/release-keys"


@pytest.fixture
def rootable(phone, fb_state):
    for slot in ("_a", "_b"):
        (phone / "by-name" / f"init_boot{slot}").write_bytes(STOCK + b"\0" * (65536 - len(STOCK)))
    props = json.loads((phone / "props.json").read_text())
    props["ro.build.fingerprint"] = FINGERPRINT
    (phone / "props.json").write_text(json.dumps(props))
    return phone


def rom_with_init_boot(tmp_path, fingerprint_product="PJZ110", init_boot=STOCK):
    parts = {"init_boot": init_boot, "boot": rf.build_boot_image(b"kernel", header_version=4), "dtbo": b"d" * 4096}
    payload = rf.build_payload(parts)
    meta = rf.ota_metadata(product=fingerprint_product)
    return open_rom(rf.build_ota_zip(tmp_path / "ota.zip", payload, meta))


def magisk_patch(phone, name="magisk_patched-28100_AbC12.img", identical=False):
    """Simulate the user tapping 'Select and Patch a File' in Magisk."""
    download = phone / "sdcard" / "Download"
    stock = (download / "opx_stock_init_boot.img").read_bytes()
    patched = stock if identical else stock.replace(b"stock-ramdisk", b"MAGISK-ramdis")
    time.sleep(0.01)
    (download / name).write_bytes(patched)
    os.utime(download / name)
    return patched


def start(adb, tmp_path, manager="magisk", rom=None):
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    rom = rom or rom_with_init_boot(tmp_path)
    partition = rt.MANAGERS[manager][1]
    stock = rt.stock_from_rom(rom, partition, device.facts, tmp_path / "work")
    session = rt.new_session(manager, device, stock, f"ROM {rom.path.name}")
    return s, device, session


def run_plan(plan, s, adb, fastboot, tmp_path):
    runner = fl.Runner(plan, None, s.profile, adb, fastboot, tmp_path / "journal.json", tmp_path / "w", wait_timeout=5)
    return runner.run()


def test_full_root_flow(adb, fastboot, rootable, fb_state, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    remote = rt.push_stock(adb, session)
    assert remote == "/sdcard/Download/opx_stock_init_boot.img"
    assert (rootable / "sdcard" / "Download" / "opx_stock_init_boot.img").read_bytes() == STOCK

    with pytest.raises(rt.RootError, match="No new patched image"):
        rt.pull_patched(adb, session, tmp_path / "work")
    patched = magisk_patch(rootable)
    assert rt.pull_patched(adb, session, tmp_path / "work") == []
    assert session.patched_remote.endswith("magisk_patched-28100_AbC12.img")

    plan = rt.prepare_flash(pn.device_from_session(s), folder, session, "root")
    assert plan.ok, plan.problems
    assert [st.label for st in plan.steps][2] == "Flash patched init_boot to the current slot"
    run_plan(plan, s, adb, fastboot, tmp_path)

    assert (rootable / "by-name" / "init_boot_a").read_bytes().startswith(patched)
    assert (rootable / "by-name" / "init_boot_b").read_bytes().startswith(STOCK)  # other slot untouched
    flashes = [e for e in fb_state.read()["log"] if e.startswith("flash")]
    assert flashes == ["flash init_boot_a"] and fb_state.read()["log"][-1] == "reboot"


def test_unroot_puts_stock_back(adb, fastboot, rootable, fb_state, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    rt.push_stock(adb, session)
    magisk_patch(rootable)
    rt.pull_patched(adb, session, tmp_path / "work")
    run_plan(rt.prepare_flash(device, folder, session, "root"), s, adb, fastboot, tmp_path)
    assert not (rootable / "by-name" / "init_boot_a").read_bytes().startswith(STOCK)

    fb_state(mode="android")
    plan = rt.prepare_flash(pn.device_from_session(s), folder, session, "unroot")
    assert plan.ok and plan.steps[2].label == "Flash stock init_boot to the current slot"
    (tmp_path / "journal.json").unlink()
    run_plan(plan, s, adb, fastboot, tmp_path)
    assert (rootable / "by-name" / "init_boot_a").read_bytes().startswith(STOCK)


def test_rom_from_another_build_is_refused(adb, rootable, tmp_path):
    s = open_session("FAKE0001", adb)
    rom = rom_with_init_boot(tmp_path, fingerprint_product="CPH2649")
    with pytest.raises(rt.RootError, match="bootloop"):
        rt.stock_from_rom(rom, "init_boot", pn.device_from_session(s).facts, tmp_path / "w")


def test_stock_from_full_backup_same_build(adb, rootable, tmp_path):
    s = open_session("FAKE0001", adb)
    folder = bk.run_backup(s.shell, bk.build_plan(s.shell, s.profile, "full", s.props), tmp_path / "b")
    facts = pn.device_from_session(s).facts
    stock = rt.stock_from_backup(folder, "init_boot", facts, tmp_path / "w")
    assert stock.read_bytes().startswith(STOCK)

    facts = dict(facts, build="PJZ110_16.0.4.600(CN01)")
    with pytest.raises(rt.RootError, match="was made on build"):
        rt.stock_from_backup(folder, "init_boot", facts, tmp_path / "w2")


def test_identical_patched_file_is_refused(adb, fastboot, rootable, fb_state, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    rt.push_stock(adb, session)
    magisk_patch(rootable, identical=True)
    problems = rt.pull_patched(adb, session, tmp_path / "work")
    assert any("identical" in p for p in problems)
    plan = rt.prepare_flash(device, folder, session, "root")
    assert not plan.ok
    with pytest.raises(fl.FlashError):
        run_plan(plan, s, adb, fastboot, tmp_path)
    assert fb_state.read()["mode"] == "android"


def test_patched_file_changed_after_check_writes_nothing(adb, fastboot, rootable, fb_state, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    rt.push_stock(adb, session)
    magisk_patch(rootable)
    rt.pull_patched(adb, session, tmp_path / "work")
    plan = rt.prepare_flash(device, folder, session, "root")
    patched = tmp_path / "work" / "patched_init_boot.img"
    patched.write_bytes(patched.read_bytes()[:-1] + b"\x01")
    with pytest.raises(fl.FlashError, match="changed since it was checked"):
        run_plan(plan, s, adb, fastboot, tmp_path)
    assert not [e for e in fb_state.read()["log"] if e.startswith("flash")]


def test_flashes_whichever_slot_is_current(adb, fastboot, rootable, fb_state, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    rt.push_stock(adb, session)
    patched = magisk_patch(rootable)
    rt.pull_patched(adb, session, tmp_path / "work")
    fb_state(slot="b")
    run_plan(rt.prepare_flash(device, folder, session, "root"), s, adb, fastboot, tmp_path)
    assert (rootable / "by-name" / "init_boot_b").read_bytes().startswith(patched)
    assert (rootable / "by-name" / "init_boot_a").read_bytes().startswith(STOCK)


def test_apatch_uses_boot(adb, rootable, tmp_path):
    for slot in ("_a", "_b"):
        (rootable / "by-name" / f"boot{slot}").write_bytes(b"\0" * 65536)
    _, _, session = start(adb, tmp_path, manager="apatch")
    assert session.partition == "boot"


def test_refused_on_cos17_and_after_build_change(adb, rootable, tmp_path):
    folder = backup_of(adb, tmp_path)
    s, device, session = start(adb, tmp_path)
    props = json.loads((rootable / "props.json").read_text())
    props["ro.build.display.id"] = "PJZ110_17.0.0.120(CN01)"
    props["ro.build.fingerprint"] = FINGERPRINT.replace("U.1", "U.2")
    (rootable / "props.json").write_text(json.dumps(props))
    plan = rt.prepare_flash(pn.device_from_session(open_session("FAKE0001", adb)), folder, session, "unroot")
    joined = " ".join(plan.problems)
    assert "Flashing is disabled" in joined and "build changed" in joined


def test_not_a_boot_image(tmp_path):
    with pytest.raises(rt.RootError, match="Not an Android boot image"):
        rt.boot_header_version(b"\0" * 128)
    with pytest.raises(rt.RootError, match="vendor_boot"):
        rt.boot_header_version(b"VNDRBOOT" + b"\0" * 120)
