import json
import os
from pathlib import Path

import pytest

import romfactory as rf
from opx import flash as fl
from opx import plan as pn
from opx.profile import load
from opx.rom.ota import open_rom
from opx.session import open_session
from test_flash import ROM_PARTS, backup_of, phone  # noqa: F401  (fixture)

IN_PARTS = {**ROM_PARTS, "oplusstanvbk": b"I" * 4096, "my_region": b"N" * 4096}
RESTORE = ["oplusstanvbk", "my_region"]


def rom_for(tmp_path, product="CPH2649", parts=None, folder=False):
    parts = parts or IN_PARTS
    meta = rf.ota_metadata(product=product)
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


CONVERT = dict(overrides=[fl.OVERRIDE_QCOM_UNVALIDATED], allow_fastbootd=True)


def test_conversion_requires_wipe(adb, fastboot, phone, tmp_path):
    plan, *_ = prepare(adb, fastboot, tmp_path, fl.FlashOptions(**CONVERT), rom=rom_for(tmp_path))
    assert any("different region" in p for p in plan.problems)


def test_conversion_keeps_own_region_files(adb, fastboot, phone, fb_state, tmp_path):
    options = fl.FlashOptions(wipe_data=True, restore_from_backup=RESTORE, **CONVERT)
    plan, runner, _, orig = prepare(adb, fastboot, tmp_path, options, rom=rom_for(tmp_path))
    assert plan.ok, plan.problems
    labels = [s.label for s in plan.steps]
    assert "Restore oplusstanvbk from your backup (both slots)" in labels
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
