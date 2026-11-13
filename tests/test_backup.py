import dataclasses
import hashlib
import json
import threading
from pathlib import Path

import pytest

from opx import backup as bk
from opx import profile as profiles
from opx.profile import PartitionRule
from opx.remote import UnsafeCommand, _assert_read_only, check_name, check_path
from opx.session import open_session


def session(adb):
    return open_session("FAKE0001", adb)


def names(plan):
    return [i.name for i in plan.items]


def test_access_detection_su(adb):
    s = session(adb)
    assert s.access.kind == "su"
    assert s.profile.id == "oneplus-13"


def test_access_detection_root_shell(adb, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "root-shell")
    assert session(adb).access.kind == "root-shell"


def test_no_root_refuses(adb, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "none")
    s = session(adb)
    assert s.access.kind == "none"
    with pytest.raises(bk.BackupError, match="Root access"):
        bk.build_plan(s.shell, s.profile, "network", s.props)


def test_network_plan(adb):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "network", s.props)
    assert names(plan) == ["oplusstanvbk_a", "oplusstanvbk_b", "my_region_a"]
    region = next(i for i in plan.items if i.name == "my_region_a")
    assert region.source == "mapper" and region.path == "/dev/block/mapper/my_region_a"
    assert region.size == 16384
    assert plan.missing == []


def test_critical_plan_includes_wildcards(adb):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "critical", s.props)
    got = set(names(plan))
    assert {"persist", "modemst1", "modemst2", "fsg", "fsc", "ocdt", "devinfo", "oplusreserve1", "oplusreserve2"} <= got
    assert "boot_a" not in got and "userdata" not in got


def test_full_plan_excludes_userdata(adb):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "full", s.props)
    got = names(plan)
    assert not {"userdata", "metadata", "frp"} & set(got)
    assert "super" in got and "boot_a" in got
    assert all(i.source == "by-name" for i in plan.items)
    assert any("personal data is not included" in w for w in plan.warnings)


def test_full_plan_skips_personal_data_even_if_the_profile_forgets(adb):
    # The exclusion is hard-coded, not left to device profiles.
    s = session(adb)
    careless = dataclasses.replace(s.profile, full_exclude=[])
    got = set(names(bk.build_plan(s.shell, careless, "full", s.props)))
    assert not {"userdata", "metadata", "frp"} & got
    assert "super" in got


def test_a_profile_rule_can_never_back_up_personal_data(adb):
    s = session(adb)
    bad = dataclasses.replace(s.profile, network=[PartitionRule("userdata", False)])
    with pytest.raises(bk.BackupError, match="personal data"):
        bk.build_plan(s.shell, bad, "network", s.props)


def test_backup_writes_verified_images_and_manifest(adb, fake_device, tmp_path):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "critical", s.props)
    events = []
    folder = bk.run_backup(s.shell, plan, tmp_path / "out", on_event=events.append)

    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["finished"] is True
    assert manifest["device"]["model"] == "PJZ110"
    for part in manifest["partitions"]:
        assert part["status"] == "ok", part
        src = next(fake_device.glob(f"*/{part['name']}"))
        img = (folder / part["file"]).read_bytes()
        assert img == src.read_bytes()
        assert part["sha256"] == hashlib.sha256(img).hexdigest() == part["device_sha256"]
    sums = (folder / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == len(manifest["partitions"])
    assert not list(folder.glob("*.part"))
    assert events[-1]["type"] == "finished" and events[-1]["failed"] == []


def test_corruption_is_detected(adb, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CORRUPT", "persist")
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "critical", s.props)
    folder = bk.run_backup(s.shell, plan, tmp_path / "out")
    manifest = json.loads((folder / "manifest.json").read_text())
    persist = next(p for p in manifest["partitions"] if p["name"] == "persist")
    assert persist["status"] == "failed" and "mismatch" in persist["error"]


def test_no_sha256sum_on_phone_is_unverified(adb, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_NO_SHA", "1")
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "network", s.props)
    folder = bk.run_backup(s.shell, plan, tmp_path / "out")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert {p["status"] for p in manifest["partitions"]} == {"unverified"}


def test_verify_folder_detects_tampering(adb, tmp_path):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "network", s.props)
    folder = bk.run_backup(s.shell, plan, tmp_path / "out")
    assert all(ok for _, ok, _ in bk.verify_folder(folder))
    img = folder / "oplusstanvbk_a.img"
    data = bytearray(img.read_bytes())
    data[10] ^= 1
    img.write_bytes(bytes(data))
    report = dict((f, ok) for f, ok, _ in bk.verify_folder(folder))
    assert report["oplusstanvbk_a.img"] is False


def test_cancel_keeps_manifest(adb, tmp_path):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "critical", s.props)
    cancel = threading.Event()

    def on_event(e):
        if e["type"] == "item_done":
            cancel.set()

    with pytest.raises(bk.Cancelled):
        bk.run_backup(s.shell, plan, tmp_path / "out", on_event=on_event, cancel=cancel)
    folder = next((tmp_path / "out").iterdir())
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["finished"] is False and len(manifest["partitions"]) == 1


def test_only_read_only_commands_reach_the_phone(adb, fake_device, tmp_path):
    s = session(adb)
    plan = bk.build_plan(s.shell, s.profile, "full", s.props)
    bk.run_backup(s.shell, plan, tmp_path / "out")
    log = (tmp_path / "commands.log").read_text().splitlines()
    for cmd in log:
        inner = cmd[7:-1] if cmd.startswith("su -c '") else cmd
        _assert_read_only(inner)  # raises if anything writable slipped through
        assert "of=" not in cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "dd if=/dev/block/by-name/boot_a of=/dev/block/by-name/boot_b",
        "rm -rf /data",
        "blockdev --setrw /dev/block/by-name/boot_a",
        "ls -1 /dev/block/by-name; reboot",
        "sha256sum /dev/block/by-name/x && reboot",
        "getprop > /sdcard/x",
    ],
)
def test_write_commands_are_refused(cmd):
    with pytest.raises(UnsafeCommand):
        _assert_read_only(cmd)


@pytest.mark.parametrize("bad", ["boot;reboot", "../etc", "a b", "$(id)", "x'y", ""])
def test_bad_partition_names_refused(bad):
    with pytest.raises(UnsafeCommand):
        check_name(bad)


def test_bad_paths_refused():
    for bad in ("/data/x", "/dev/block/by-name/../../x", "/dev/block/by-name/a b"):
        with pytest.raises(UnsafeCommand):
            check_path(bad)


def test_never_flash_list():
    prof = profiles.load("oneplus-13")
    for p in ("persist", "modemst1", "modemst2", "fsg", "ocdt", "oplusreserve2", "userdata", "devinfo"):
        assert prof.is_never_flash(p), p
    for p in ("boot_a", "init_boot_b", "vendor_boot_a", "my_region_a"):
        assert not prof.is_never_flash(p), p


def test_known_issue_for_coloros_17_cn():
    prof = profiles.detect({"ro.product.model": "PJZ110"})
    assert prof.issues_for({"ro.build.display.id": "PJZ110_17.0.0.120(CN01)"})
    assert not prof.issues_for({"ro.build.display.id": "PJZ110_16.0.3.501(CN01)"})


def test_unknown_model_uses_generic_profile():
    assert profiles.detect({"ro.product.model": "SOMETHING"}).id == "oplus-generic"


def test_empty_space_is_not_stored_but_reads_back_identically(adb, tmp_path):
    import os
    import random

    from conftest import PHYSICAL

    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    size = PHYSICAL["super"]
    # mostly empty, with data at the start, in the middle and at the very end
    body = bytearray(size)
    body[:4096] = random.Random(1).randbytes(4096)
    body[size // 2: size // 2 + 100] = b"x" * 100
    body[-1] = 7
    (phone / "by-name" / "super").write_bytes(bytes(body))
    s = session(adb)
    folder = bk.run_backup(s.shell, bk.build_plan(s.shell, s.profile, "full", s.props), tmp_path / "b")
    img = folder / "super.img"
    assert img.read_bytes() == bytes(body)
    manifest = json.loads((folder / "manifest.json").read_text())
    entry = next(p for p in manifest["partitions"] if p["name"] == "super")
    assert entry["sha256"] == hashlib.sha256(body).hexdigest() and entry["status"] == "ok"


def test_trailing_empty_space_keeps_the_full_size(adb, tmp_path):
    import os

    from conftest import PHYSICAL

    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    (phone / "by-name" / "super").write_bytes(b"data" + b"\0" * (PHYSICAL["super"] - 4))
    s = session(adb)
    folder = bk.run_backup(s.shell, bk.build_plan(s.shell, s.profile, "full", s.props), tmp_path / "b")
    assert (folder / "super.img").stat().st_size == PHYSICAL["super"]


def test_disk_usage_is_not_more_than_size(tmp_path):
    f = tmp_path / "x.img"
    with open(f, "wb") as fh:
        bk.make_sparse(fh)
        fh.seek(8 * 1024 * 1024)
        fh.write(b"end")
    assert f.stat().st_size == 8 * 1024 * 1024 + 3
    assert 0 < bk.disk_usage(f) <= f.stat().st_size + 65536


def test_full_backup_refused_while_an_update_merges(adb, tmp_path):
    import os

    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    (phone / "mapper" / "system_a-cow").write_bytes(b"\0" * 512)
    s = session(adb)
    with pytest.raises(bk.BackupError, match="update is still being applied"):
        bk.build_plan(s.shell, s.profile, "full", s.props)
    # Critical backups read logical partitions through the snapshot view and stay allowed
    assert bk.build_plan(s.shell, s.profile, "critical", s.props).items
