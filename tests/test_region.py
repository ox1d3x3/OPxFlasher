import json
import os
from pathlib import Path

import pytest
import romfactory as rf
from test_flash import ROM_PARTS, backup_of, phone  # noqa: F401  (fixture)

from opx import flash as fl
from opx import plan as pn
from opx.profile import load
from opx.rom.ota import open_rom
from opx.session import open_session

IN_PARTS = {**ROM_PARTS, "oplusstanvbk": b"I" * 4096, "my_region": b"N" * 4096}
RESTORE = ["oplusstanvbk", "my_region"]


def rom_for(tmp_path, product="CPH2649", parts=None, folder=False, codename="testdevice"):
    parts = parts or IN_PARTS
    meta = rf.ota_metadata(product=product, codename=codename)
    if folder:
        d = tmp_path / "romdir"
        (d / "META-INF" / "com" / "android").mkdir(parents=True)
        (d / "META-INF" / "com" / "android" / "metadata").write_text("".join(f"{k}={v}\n" for k, v in meta.items()))
        for name, data in parts.items():
            (d / f"{name}.img").write_bytes(data)
        return open_rom(d)
    payload = rf.build_payload(parts, groups={"main": [n for n in ("system", "my_region") if n in parts]})
    return open_rom(rf.build_ota_zip(tmp_path / "in.zip", payload, meta))


def simulate_ota_overwrite(phone):
    """An OTA on the converted phone replaced the region files with foreign ones."""
    for slot in ("_a", "_b"):
        f = phone / "by-name" / f"oplusstanvbk{slot}"
        f.write_bytes(b"X" * f.stat().st_size)
    (phone / "mapper" / "my_region_a").write_bytes(b"Y" * 16384)


def prepare(adb, fastboot, tmp_path, options, rom=None, backup="critical"):
    folder = backup_of(adb, tmp_path, backup)
    root = Path(os.environ["FAKE_DEVICE_DIR"])
    originals = {
        "stan_a": (root / "by-name" / "oplusstanvbk_a").read_bytes(),
        "stan_b": (root / "by-name" / "oplusstanvbk_b").read_bytes(),
        "region": (root / "mapper" / "my_region_a").read_bytes(),
    }
    s = open_session("FAKE0001", adb)
    device = pn.device_from_session(s)
    if rom is None:
        plan = fl.prepare_restore(device, folder, options)
    else:
        plan = fl.prepare(rom, pn.build_report(rom, device), device, options, folder)
    runner = fl.Runner(plan, rom, s.profile, adb, fastboot, tmp_path / "journal.json", tmp_path / "work", wait_timeout=5)
    return plan, runner, folder, originals


CONVERT = dict(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED, fl.OVERRIDE_VARIANT], allow_fastbootd=True)


def test_conversion_requires_wipe(adb, fastboot, phone, tmp_path):
    plan, *_ = prepare(adb, fastboot, tmp_path, fl.FlashOptions(**CONVERT), rom=rom_for(tmp_path))
    assert any("different region" in p for p in plan.problems)


def test_conversion_keeps_own_region_files(adb, fastboot, phone, fb_state, tmp_path):
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, runner, _, orig = prepare(adb, fastboot, tmp_path, options, rom=rom_for(tmp_path))
    assert plan.ok, plan.problems
    labels = [s.label for s in plan.steps]
    assert "Restore oplusstanvbk from your backup (both slots, in fastbootd)" in labels  # OnePlus 13
    assert "Restore my_region from your backup (logical, in fastbootd)" in labels
    simulate_ota_overwrite(phone)  # prove the restore really writes
    runner.run()

    assert (phone / "by-name" / "oplusstanvbk_a").read_bytes() == orig["stan_a"]
    assert (phone / "by-name" / "oplusstanvbk_b").read_bytes() == orig["stan_b"]
    assert (phone / "mapper" / "my_region_a").read_bytes() == orig["region"]
    assert (phone / "mapper" / "system_a").read_bytes() == rf.pad(ROM_PARTS["system"])
    assert fb_state.read()["wiped"] is True


def test_super_image_path_restores_my_region_through_fastbootd(adb, fastboot, phone, fb_state, tmp_path):
    parts = {k: v for k, v in IN_PARTS.items() if k not in ("system", "my_region")}
    parts["super"] = b"P" * 8192
    rom = rom_for(tmp_path, parts=parts, folder=True)
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, runner, _, orig = prepare(adb, fastboot, tmp_path, options, rom=rom)
    kinds = [s.kind for s in plan.steps]
    assert kinds.index("flash-super") < kinds.index("to-fastbootd")
    simulate_ota_overwrite(phone)
    runner.run()
    assert (phone / "super.flashed").read_bytes() == b"P" * 8192
    assert (phone / "mapper" / "my_region_a").read_bytes() == orig["region"]


def test_restoring_my_region_needs_fastbootd(adb, fastboot, phone, tmp_path):
    options = fl.FlashOptions(restore_from_backup=["my_region"])
    plan, *_ = prepare(adb, fastboot, tmp_path, options)
    assert any("needs fastbootd" in p for p in plan.problems)


def test_restore_only_after_an_ota(adb, fastboot, phone, fb_state, tmp_path):
    options = fl.FlashOptions(restore_from_backup=RESTORE, allow_fastbootd=True)
    plan, runner, _, orig = prepare(adb, fastboot, tmp_path, options)
    assert plan.ok, plan.problems
    assert plan.rom_path == ""
    simulate_ota_overwrite(phone)
    runner.run()
    assert (phone / "by-name" / "oplusstanvbk_a").read_bytes() == orig["stan_a"]
    assert (phone / "mapper" / "my_region_a").read_bytes() == orig["region"]
    flashes = [e for e in fb_state.read()["log"] if e.startswith("flash")]
    assert flashes == ["flash oplusstanvbk_a", "flash oplusstanvbk_b", "flash my_region"]
    assert fb_state.read()["log"][-1] == "reboot"


def test_tampered_backup_writes_nothing(adb, fastboot, phone, fb_state, tmp_path):
    options = fl.FlashOptions(restore_from_backup=RESTORE, allow_fastbootd=True)
    plan, runner, folder, _ = prepare(adb, fastboot, tmp_path, options)
    img = folder / "oplusstanvbk_b.img"
    img.write_bytes(b"\xff" + img.read_bytes()[1:])
    with pytest.raises(fl.FlashError, match="doesn't match the hash"):
        runner.run()
    assert fb_state.read()["log"] == [] and fb_state.read()["mode"] == "android"


def test_only_region_files_can_be_restored(adb, fastboot, phone, tmp_path):
    options = fl.FlashOptions(restore_from_backup=["persist", "modemst1"], allow_fastbootd=True)
    plan, *_ = prepare(adb, fastboot, tmp_path, options)
    assert sum("can't be restored" in p for p in plan.problems) == 2


def test_rom_and_backup_for_same_partition_is_refused(adb, fastboot, phone, tmp_path):
    options = fl.FlashOptions(wipe_data=True, include_optional=["oplusstanvbk"], restore_from_backup=RESTORE, **CONVERT)
    plan, *_ = prepare(adb, fastboot, tmp_path, options, rom=rom_for(tmp_path))
    assert any("both the ROM and your backup" in p for p in plan.problems)


def test_restore_refused_on_cos17(adb, fastboot, phone, tmp_path):
    props = json.loads((phone / "props.json").read_text())
    props["ro.build.display.id"] = "PJZ110_17.0.0.120(CN01)"
    (phone / "props.json").write_text(json.dumps(props))
    plan, *_ = prepare(adb, fastboot, tmp_path, fl.FlashOptions(restore_from_backup=RESTORE, allow_fastbootd=True))
    assert any("Restoring is disabled" in p for p in plan.problems)


def test_restore_resumes_after_failure(adb, fastboot, phone, fb_state, tmp_path):
    fb_state(fail_flash="my_region")
    options = fl.FlashOptions(restore_from_backup=RESTORE, allow_fastbootd=True)
    plan, runner, _, orig = prepare(adb, fastboot, tmp_path, options)
    simulate_ota_overwrite(phone)
    with pytest.raises(fl.FlashError):
        runner.run()
    assert fb_state.read()["mode"] == "fastbootd"
    fb_state(fail_flash=None)
    fl.resume_runner(tmp_path / "journal.json", load("oneplus-13"), adb, fastboot, wait_timeout=5).run()
    assert (phone / "mapper" / "my_region_a").read_bytes() == orig["region"]
    assert fb_state.read()["log"].count("flash oplusstanvbk_a") == 1


def test_region_file_checked_by_the_roms_vbmeta_always_comes_from_the_rom(adb, fastboot, phone, tmp_path):
    """my_region protected by the new ROM's vbmeta: keeping or restoring the old copy would fail
    verification at boot, so it's written from the ROM; oplusstanvbk can still be restored."""
    region = b"R" * 4096
    parts = {**IN_PARTS, "my_region": region,
             "vbmeta": rf.build_vbmeta(1, hashtrees=[("my_region", region)])}
    rom = rom_for(tmp_path, parts=parts)
    s = open_session("FAKE0001", adb)
    report = pn.build_report(rom, pn.device_from_session(s))
    item = next(i for i in report.items if i.partition == "my_region")
    assert item.action == "super" and "vbmeta checks it" in item.note
    assert fl.keepable(report, RESTORE) == ["oplusstanvbk"]
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, *_ = prepare(adb, fastboot, tmp_path, options, rom=rom)
    assert any("my_region can't come from your backup" in p for p in plan.problems)
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=fl.keepable(report, RESTORE), **CONVERT)
    (tmp_path / "b2").mkdir()
    plan, *_ = prepare(adb, fastboot, tmp_path / "b2", options, rom=rom)
    assert not any("my_region" in p for p in plan.problems)
    labels = " ".join(st.label for st in plan.steps)
    assert "oplusstanvbk" in labels


def test_odm_is_always_flashed_with_the_rom(adb, phone, tmp_path):
    rom = rom_for(tmp_path, parts={**IN_PARTS, "odm": b"O" * 8192})
    report = pn.build_report(rom, pn.device_from_session(open_session("FAKE0001", adb)))
    odm = next(i for i in report.items if i.partition == "odm")
    assert odm.action in ("flash", "super") and "build content" in odm.note
    assert fl.keepable(report, ["odm"]) == []


def set_props(phone, **changes):
    props = json.loads((phone / "props.json").read_text())
    props.update(changes)
    (phone / "props.json").write_text(json.dumps(props))


def test_china_phone_converts_to_global_with_real_codenames(adb, fastboot, phone, fb_state, tmp_path):
    """A PJZ110 (OP5D0DL1) taking a CPH2653 (OP5D55L1) ROM: the same phone in another region, so it
    reaches the Variant check (confirmable) instead of a Device match STOP. Afterwards the phone reports
    CPH2653, and the backup made before the conversion still restores its signal files."""
    set_props(phone, **{"ro.product.device": "OP5D0DL1", "ro.product.model": "PJZ110", "ro.product.name": "PJZ110"})
    rom = rom_for(tmp_path, product="CPH2653", codename="OP5D55L1")
    device = pn.device_from_session(open_session("FAKE0001", adb))
    report = pn.build_report(rom, device)
    checks = {c.name: c for c in report.checks}
    assert checks["Device match"].status == "pass" and "other region" in checks["Device match"].detail
    assert checks["Variant"].status == "block" and fl.region_change(report)

    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, runner, folder, orig = prepare(adb, fastboot, tmp_path, options, rom=rom)
    assert plan.ok, plan.problems
    runner.run()
    assert fb_state.read()["wiped"] is True
    assert (phone / "mapper" / "system_a").read_bytes() == rf.pad(ROM_PARTS["system"])

    # now running Global firmware: model and codename changed, serial didn't
    set_props(phone, **{"ro.product.device": "OP5D55L1", "ro.product.model": "CPH2653", "ro.product.name": "CPH2653"})
    simulate_ota_overwrite(phone)
    converted = pn.device_from_session(open_session("FAKE0001", adb))
    assert converted.facts["model"] == "CPH2653"
    assert fl.check_backup(folder, converted.facts, converted.profile) == []
    restore = fl.prepare_restore(converted, folder, fl.FlashOptions(restore_from_backup=RESTORE, allow_fastbootd=True))
    assert restore.ok, restore.problems


def test_other_phone_codenames_still_block(adb, phone, tmp_path):
    set_props(phone, **{"ro.product.device": "OP5D0DL1"})
    rom = rom_for(tmp_path, product="CPH2653", codename="OP5D77L1")  # not a OnePlus 13 codename
    r = pn.build_report(rom, pn.device_from_session(open_session("FAKE0001", adb)))
    assert next(c for c in r.checks if c.name == "Device match").status == "block"


def test_backup_model_change_needs_same_serial_and_known_variants(adb, phone, tmp_path):
    folder = backup_of(adb, tmp_path)
    prof = load("oneplus-13")
    ok = {"serial": "FAKE0001", "model": "CPH2653"}
    assert fl.check_backup(folder, ok, prof) == []
    assert any("different phone" in p for p in fl.check_backup(folder, ok))  # no profile: model must match
    assert any("model" in p for p in fl.check_backup(folder, {"serial": "FAKE0001", "model": "CPH9999"}, prof))
    assert any("serial" in p for p in fl.check_backup(folder, {"serial": "OTHER", "model": "PJZ110"}, prof))
    assert any("model" in p for p in fl.check_backup(folder, {"serial": "OTHER", "model": "CPH2653"}, prof))


def test_conversion_wipes_before_the_new_bootloader_and_ends_in_fastbootd(adb, fastboot, phone, fb_state, tmp_path):
    """A 17 abl may leave bootloader fastboot within a second on CN hardware: nothing after the first
    write may need the bootloader's fastboot mode."""
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, runner, *_ = prepare(adb, fastboot, tmp_path, options, rom=rom_for(tmp_path))
    kinds = [s.kind for s in plan.steps]
    assert kinds[:3] == ["to-bootloader", "preflight", "wipe"]
    first_write = next(i for i, k in enumerate(kinds) if k.startswith("flash"))
    assert all(s.mode != "bootloader" for s in plan.steps[first_write:] if s.kind.startswith(("to-", "reboot", "wipe"))
               and s.kind != "to-fastbootd")
    assert kinds[-1] == "reboot-from-fastbootd" and "back-to-bootloader" not in kinds
    runner.run()
    log = fb_state.read()["log"]
    assert log.index(next(e for e in log if e.startswith("wipe") or e.startswith("erase") or "-w" in e)) < \
        log.index(next(e for e in log if e.startswith("flash")))
