import json

import pytest
import romfactory as rf

from opx import backup as bk
from opx import plan as pn
from opx.rom.ota import open_rom
from opx.session import open_session


def sized(data, size=64 * 1024):
    """Pad like a real partition, which is larger than the image in it."""
    return data + b"\0" * (size - len(data))


def put(fake_device, name, data):
    (fake_device / "by-name" / name).write_bytes(data)


@pytest.fixture
def phone(fake_device):
    """Fake phone with vbmeta (rollback 2) and xbl_config (ARB 1) on both slots."""
    for slot in ("_a", "_b"):
        put(fake_device, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(fake_device, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(fake_device, f"dtbo{slot}", b"\0" * (64 * 1024))
    return fake_device


def rom_zip(tmp_path, vb_index=2, arb=1, codename="testdevice", product="PJZ110", extra=None, incremental=False, dtbo_size=4096,
            meta=None):
    parts = {
        "vbmeta": rf.build_vbmeta(vb_index),
        "xbl_config": rf.build_qcom_elf(arb=arb),
        "dtbo": b"d" * dtbo_size,
        "system": b"s" * 8192,
        "my_region": b"r" * 4096,
        "oplusstanvbk": b"o" * 4096,
    }
    parts.update(extra or {})
    # incremental: delta data for system (the metadata label alone no longer decides)
    payload = rf.build_payload(parts, groups={"main": ["system", "my_region"]},
                               incremental={"system"} if incremental else None)
    metadata = rf.ota_metadata(codename=codename, product=product, incremental=incremental, **(meta or {}))
    return open_rom(rf.build_ota_zip(tmp_path / "rom.zip", payload, metadata))


def report_for(adb, rom):
    return pn.build_report(rom, pn.device_from_session(open_session("FAKE0001", adb)))


def status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def test_matching_rom_is_blocked_only_by_unvalidated_qcom_arb(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path))
    assert status(r, "Package") == "pass"
    assert status(r, "Device match") == "pass"
    assert status(r, "AVB vbmeta") == "pass"
    assert status(r, "Qualcomm ARB xbl_config") == "block"
    assert "not yet validated" in next(c.detail for c in r.checks if c.name == "Qualcomm ARB xbl_config")
    assert status(r, "Partition sizes") == "pass"
    assert r.verdict == "BLOCKED"
    actions = {i.partition: i.action for i in r.items}
    assert actions["system"] == "super" and actions["my_region"] == "conditional"
    assert actions["oplusstanvbk"] == "conditional" and actions["dtbo"] == "flash"


def test_validated_profile_allows_equal_arb(adb, phone, tmp_path, monkeypatch):
    s = open_session("FAKE0001", adb)
    s.profile.qcom_arb_validated = True
    r = pn.build_report(rom_zip(tmp_path), pn.device_from_session(s))
    assert status(r, "Qualcomm ARB xbl_config") == "pass"
    assert r.verdict == "OK"


def test_lower_arb_blocks_even_when_validated(adb, phone, tmp_path):
    s = open_session("FAKE0001", adb)
    s.profile.qcom_arb_validated = True
    for slot in ("_a", "_b"):
        put(phone, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=2)))
    r = pn.build_report(rom_zip(tmp_path, arb=1), pn.device_from_session(s))
    assert status(r, "Qualcomm ARB xbl_config") == "block"
    assert "hard-brick" in next(c.detail for c in r.checks if c.name.startswith("Qualcomm"))


def test_avb_rollback_lower_blocks(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path, vb_index=1))
    assert status(r, "AVB vbmeta") == "block"


def test_wrong_device_blocks(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path, codename="otherphone"))
    assert status(r, "Device match") == "block"


def test_other_variant_blocks_until_confirmed(adb, phone, tmp_path):
    from opx import flash as fl

    r = report_for(adb, rom_zip(tmp_path, product="CPH2653"))
    assert status(r, "Variant") == "block"
    detail = next(c.detail for c in r.checks if c.name == "Variant")
    assert "OnePlus 13 Global (CPH2653)" in detail and "OnePlus 13 China (PJZ110)" in detail
    assert fl.region_change(r)
    assert [c.name for c in fl.hard_blocks(r, [fl.OVERRIDE_VARIANT]) if c.name == "Variant"] == []


def test_same_variant_passes(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path))
    assert status(r, "Variant") == "pass"


def test_incremental_blocks(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path, incremental=True))
    assert status(r, "Package") == "block"


def test_oversized_image_blocks(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path, dtbo_size=128 * 1024))
    assert status(r, "Partition sizes") == "block"


def test_never_flash_partitions_are_skipped(adb, phone, tmp_path):
    r = report_for(adb, rom_zip(tmp_path, extra={"persist": b"p" * 4096, "modemst1": b"m" * 4096}))
    actions = {i.partition: i.action for i in r.items}
    assert actions["persist"] == "skip" and actions["modemst1"] == "skip"
    assert status(r, "Protected partitions") == "info"


def test_no_device_blocks(tmp_path):
    from opx import flash as fl

    r = pn.build_report(rom_zip(tmp_path), pn.no_device())
    assert status(r, "Phone") == "block" and r.verdict == "NOT COMPARED"
    # only the ROM's own checks: one stop (connect the phone), nothing guessed about the phone
    assert [c.name for c in r.checks if c.status == "block"] == ["Phone"]
    names = {c.name for c in r.checks}
    assert "Device match" not in names and not any(n.startswith(("AVB ", "Qualcomm ARB")) for n in names)
    assert status(r, "For") == "info" and "rollback indexes" in next(c.detail for c in r.checks if c.name == "Anti-rollback")
    assert r.checks[0].name == "Phone"  # problems first
    assert fl.hard_blocks(r, [fl.OVERRIDE_QCOM_UNVALIDATED]) != []  # still can't be flashed


def test_no_device_still_blocks_a_bad_rom(tmp_path):
    r = pn.build_report(rom_zip(tmp_path, incremental=True), pn.no_device())
    assert r.verdict == "BLOCKED" and r.checks[0].status == "block"


def test_checks_are_sorted_problems_first():
    C = pn.Check
    out = pn.sort_checks([C("a", "info", ""), C("b", "pass", ""), C("c", "block", ""), C("d", "warn", ""), C("e", "block", "")])
    assert [c.name for c in out] == ["c", "e", "d", "b", "a"]


def test_cos17_device_is_blocked(adb, phone, tmp_path):
    props = json.loads((phone / "props.json").read_text())
    props["ro.build.display.id"] = "PJZ110_17.0.0.120(CN01)"
    (phone / "props.json").write_text(json.dumps(props))
    r = report_for(adb, rom_zip(tmp_path))
    assert status(r, "Known issue") == "block"


def test_plan_from_full_backup(adb, phone, tmp_path):
    s = open_session("FAKE0001", adb)
    plan = bk.build_plan(s.shell, s.profile, "full", s.props)
    folder = bk.run_backup(s.shell, plan, tmp_path / "backups")
    dev = pn.device_from_backup(folder)
    assert dev.source == "backup" and dev.size_of("dtbo") == 64 * 1024
    r = pn.build_report(rom_zip(tmp_path), dev)
    assert status(r, "Device match") == "pass"
    assert status(r, "AVB vbmeta") == "pass"
    assert json.loads(r.to_json())["verdict"] == "BLOCKED"  # qcom ARB not validated yet


def test_read_only_while_planning(adb, phone, tmp_path):
    from opx.remote import _assert_read_only

    report_for(adb, rom_zip(tmp_path))
    for cmd in (tmp_path / "commands.log").read_text().splitlines():
        _assert_read_only(cmd[7:-1] if cmd.startswith("su -c '") else cmd)


# --- issue #45: hardware project, and phones without root -------------------------------

PROJECT_META = {"oplus_project_numbers": "24875", "oplus_hex_nv_id": "1B"}


def set_props(phone, **changes):
    props = json.loads((phone / "props.json").read_text())
    props.update(changes)
    (phone / "props.json").write_text(json.dumps(props))


def detail(report, name):
    return next(c.detail for c in report.checks if c.name == name)


def test_nv_hex():
    assert pn.nv_hex("00011011") == "1B"  # ro.build.oplus_nv_id, as bits
    assert pn.nv_hex("1B") == "1B" and pn.nv_hex("1b") == "1B"  # an OTA's oplus_hex_nv_id
    assert pn.nv_hex("") == "" and pn.nv_hex("zz") == "ZZ"


def test_same_project_passes(adb, phone, tmp_path):
    set_props(phone, **{"ro.boot.prjname": "24875", "ro.build.oplus_nv_id": "00011011"})
    r = report_for(adb, rom_zip(tmp_path, meta=PROJECT_META))
    assert status(r, "Project") == "pass"
    assert "NV id: ROM 1B, phone 1B" in detail(r, "Project")


def test_cross_flashed_phone_is_warned(adb, phone, tmp_path):
    """A 13T board (24821) running 13s India firmware: same product, other hardware project."""
    set_props(phone, **{"ro.boot.prjname": "24821", "ro.build.oplus_nv_id": "00011011"})
    r = report_for(adb, rom_zip(tmp_path, meta=PROJECT_META))
    assert status(r, "Project") == "warn"
    text = detail(r, "Project")
    assert "project 24875" in text and "project 24821" in text
    assert "cross-flashed" in text and "Local install" in text and "relock" in text


def test_project_list_and_missing_facts(adb, phone, tmp_path):
    set_props(phone, **{"ro.boot.prjname": "24821"})
    r = report_for(adb, rom_zip(tmp_path, meta={"oplus_project_numbers": "24821,24875"}))
    assert status(r, "Project") == "pass" and "NV id" not in detail(r, "Project")
    # a ROM that names no project, or a phone that reports none: no check at all
    r = report_for(adb, rom_zip(tmp_path))
    assert "Project" not in {c.name for c in r.checks}


def test_phone_without_root_is_compared_from_props(adb, phone, tmp_path, monkeypatch):
    from opx import flash as fl

    monkeypatch.setenv("FAKE_ACCESS", "none")
    set_props(phone, **{"ro.boot.prjname": "24821", "ro.build.oplus_nv_id": "00011011"})
    session = open_session("FAKE0001", adb)
    dev = pn.device_for_session(session)
    assert dev.source == "props" and "without root" in dev.label
    r = pn.build_report(rom_zip(tmp_path, meta=PROJECT_META), dev)
    # no root is no longer a stop: what Android reports is compared, the rest is confirmed by the user
    assert status(r, "Root") == "info" and status(r, "Phone") == "info"
    assert status(r, "Device match") == "pass" and status(r, "Project") == "warn"
    names = {c.name for c in r.checks}
    assert not any(n.startswith("AVB ") for n in names) and "Partition sizes" not in names
    assert "with root" in detail(r, "Anti-rollback")
    # Qualcomm ARB can't be read: a STOP only the user's ARB CHECKED lifts
    arb = [c for c in r.checks if c.name.startswith("Qualcomm ARB")]
    assert arb and all(c.status == "block" and "can't be read without root" in c.detail for c in arb)
    assert fl.hard_blocks(r, []) != [] and fl.hard_blocks(r, [fl.OVERRIDE_QCOM_UNVALIDATED]) == []


def test_phone_in_fastboot_is_compared_with_what_it_reported_in_android(adb, phone, tmp_path):
    props = open_session("FAKE0001", adb).props
    dev = pn.device_from_fastboot("FAKE0001", props, "fastbootd")
    assert dev.source == "fastboot" and "in fastbootd" in dev.label and "reported in Android" in dev.label
    r = pn.build_report(rom_zip(tmp_path), dev)
    assert status(r, "Device match") == "pass" and status(r, "Root") == "info"
    assert r.compared and "Partition sizes" not in {c.name for c in r.checks}
    # a wrong ROM is still refused
    r = pn.build_report(rom_zip(tmp_path, codename="otherphone"), dev)
    assert status(r, "Device match") == "block"


def test_phone_in_fastboot_never_seen_in_android_is_not_compared(tmp_path):
    dev = pn.no_device("The phone is in fastbootd, where its installed build can't be read. Start it in Android")
    r = pn.build_report(rom_zip(tmp_path), dev)
    assert r.verdict == "NOT COMPARED" and "Start it in Android" in detail(r, "Phone")


def test_recovery_props_are_not_android():
    assert pn.is_recovery({"ro.build.display.id": "aera_dodge-eng 99.87.36", "ro.product.device": "ossi"})
    assert pn.is_recovery({"ro.bootmode": "recovery"})
    assert pn.is_recovery({"ro.twrp.version": "3.7"})
    assert not pn.is_recovery({"ro.build.display.id": "CPH2653_16.0.10.501(EX01)", "ro.bootmode": "normal"})


def test_rooted_phone_still_reads_partitions(adb, phone):
    assert pn.device_for_session(open_session("FAKE0001", adb)).source == "live"


def test_phone_known_only_from_the_bootloader(tmp_path):
    # a OnePlus 13 whose Android doesn't start: fastboot says "dodge"; the build and region are the user's call
    from opx import flash as fl
    from opx import profile as profiles

    dev = pn.device_from_fastboot("SER1", None, "fastbootd", {"product": "dodge", "current-slot": "a"})
    assert dev.profile.id == "oneplus-13" and dev.slot == "_a" and dev.source == "fastboot"
    r = pn.build_report(rom_zip(tmp_path, codename="OP5D55L1", product="CPH2653"), dev)
    assert status(r, "Device match") == "pass" and "dodge" in detail(r, "Device match")
    assert status(r, "Variant") == "warn"
    assert status(r, "Installed build") == "block" and "same or a newer build" in detail(r, "Installed build")
    assert r.compared
    # the installed-build and ARB confirmations are the only stops, both lifted by the warning
    assert fl.hard_blocks(r, list(fl.RISK_OVERRIDES)) == []
    # another phone's ROM is still refused
    r = pn.build_report(rom_zip(tmp_path, codename="OP612BL1", product="CPH2723"), dev)
    assert status(r, "Device match") == "block"
    # a bootloader name nobody knows: not this phone, nothing to flash
    unknown = pn.device_from_fastboot("SER1", None, "fastboot", {"product": "mystery"})
    assert unknown.profile.id == profiles.GENERIC
    r = pn.build_report(rom_zip(tmp_path, codename="OP5D55L1", product="CPH2653"), unknown)
    # not identified: the owner's confirmation, never a silent pass
    assert status(r, "ROM identity") == "block" and "'mystery'" in detail(r, "ROM identity")


def test_loose_images_are_the_owners_call(tmp_path):
    # a folder of .img files with no OTA metadata: which phone / build it is, the owner confirms
    from opx import flash as fl

    folder = tmp_path / "imgs"
    folder.mkdir()
    for name, data in {"vbmeta": rf.build_vbmeta(2), "dtbo": b"d" * 4096, "system": b"s" * 8192}.items():
        (folder / f"{name}.img").write_bytes(data)
    rom = open_rom(folder)
    dev = pn.device_from_fastboot("SER1", None, "fastbootd", {"product": "dodge", "current-slot": "a"})
    r = pn.build_report(rom, dev)
    assert status(r, "ROM identity") == "block" and "OnePlus 13" in detail(r, "ROM identity")
    assert fl.hard_blocks(r, list(fl.RISK_OVERRIDES)) == []  # all lifted by the owner's confirmation
    # an unknown phone: never
    unknown = pn.device_from_fastboot("SER1", None, "fastboot", {"product": "mystery"})
    assert status(pn.build_report(rom, unknown), "Device match") == "block"  # loose images AND unknown phone


def test_bootloader_that_only_names_the_chip_platform(tmp_path):
    # real OnePlus 13 bootloader on a second PC: getvar product = "sun" (the SM8750 platform)
    from opx import flash as fl

    rom = rom_zip(tmp_path, codename="OP5D55L1", product="CPH2653")
    dev = pn.device_from_fastboot("SER1", None, "fastboot", {"product": "sun", "current-slot": "a", "unlocked": "yes"})
    assert dev.profile.id == "oplus-generic"  # a platform name is never a phone's identity
    dev = pn.adopt_rom_profile(rom, dev)
    assert dev.profile.id == "oneplus-13" and dev.profile.firmware_in_fastbootd  # flash order + fuse rules
    r = pn.build_report(rom, dev)
    assert status(r, "ROM identity") == "block" and "'sun'" in detail(r, "ROM identity")
    assert status(r, "Installed build") == "block"
    assert fl.hard_blocks(r, list(fl.RISK_OVERRIDES)) == []  # all the owner's call in the warning
    assert fl.hard_blocks(r, []) != []  # but never without it
