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


def rom_zip(tmp_path, vb_index=2, arb=1, codename="testdevice", product="PJZ110", extra=None, incremental=False, dtbo_size=4096):
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
    meta = rf.ota_metadata(codename=codename, product=product, incremental=incremental)
    return open_rom(rf.build_ota_zip(tmp_path / "rom.zip", payload, meta))


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
    assert "OnePlus 13 EU (CPH2653)" in detail and "OnePlus 13 China (PJZ110)" in detail
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
