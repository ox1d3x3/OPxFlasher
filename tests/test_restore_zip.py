"""Dynamic-partition metadata and the recovery restore zip."""

import hashlib
import io
import json
import zipfile

import pytest

from opx import lp
from opx import restore as rs
from opx import restore_zip as rz
from test_flash import backup_of, phone  # noqa: F401  (fixture)

SECTOR = lp.SECTOR


def make_super(contents: dict[str, bytes], size: int, slot: str = "_a", gap: bool = False) -> bytes:
    """A super image holding `contents` (bare names) in `slot`, plus empty other-slot entries."""
    md = lp.Metadata(metadata_max_size=4096, slot_count=2, block_size=4096, minor=2, flags=1)
    other = "_b" if slot == "_a" else "_a"
    md.groups = [lp.Group("default", 0, 0), lp.Group("qti_dynamic_partitions_a", 1, size // 2),
                 lp.Group("qti_dynamic_partitions_b", 1, size // 2)]
    g = {"_a": 1, "_b": 2}
    head_len = len(lp.write_super_head(md))
    first = -(-head_len // 4096) * 4096 // SECTOR
    md.block_devices = [lp.BlockDevice(first, 4096, 0, size, "super", 0)]
    data, sector = {}, first
    for name, body in contents.items():
        assert len(body) % SECTOR == 0
        n = len(body) // SECTOR
        extents = [lp.Extent(n, lp.EXTENT_LINEAR, sector)]
        if gap:  # second half as a zero extent, to exercise both types
            extents = [lp.Extent(n // 2, lp.EXTENT_LINEAR, sector), lp.Extent(n - n // 2, lp.EXTENT_ZERO)]
        md.partitions.append(lp.Partition(name + slot, lp.ATTR_READONLY | 0x2, g[slot], extents))
        md.partitions.append(lp.Partition(name + other, lp.ATTR_READONLY, g[other], []))
        data[sector] = body
        sector += n
    img = bytearray(size)
    head = lp.write_super_head(md)
    img[: len(head)] = head
    for s, body in data.items():
        img[s * SECTOR: s * SECTOR + len(body)] = body
    return bytes(img)


SYSTEM = b"S" * 8192
REGION = b"R" * 4096


def test_super_round_trip_and_copy():
    img = make_super({"system": SYSTEM, "my_region": REGION}, 131072)
    md = lp.read_super(io.BytesIO(img), 0)
    assert {p.name for p in md.partitions} == {"system_a", "system_b", "my_region_a", "my_region_b"}
    out = io.BytesIO()
    sha = lp.copy_partition(io.BytesIO(img), md.partition("system_a"), out)
    assert out.getvalue() == SYSTEM and sha == hashlib.sha256(SYSTEM).hexdigest()


def test_zero_extents_are_written_as_zeros():
    img = make_super({"system": SYSTEM}, 131072, gap=True)
    md = lp.read_super(io.BytesIO(img), 0)
    out = io.BytesIO()
    lp.copy_partition(io.BytesIO(img), md.partition("system_a"), out)
    assert out.getvalue() == SYSTEM[:4096] + b"\0" * 4096


def test_slot_b_and_backup_copy():
    img = bytearray(make_super({"system": SYSTEM}, 131072, slot="_b"))
    md = lp.read_super(io.BytesIO(bytes(img)), 1)
    assert md.partition("system_b").size == len(SYSTEM)
    primary_slot1 = lp.RESERVED + 2 * lp.GEOMETRY_SIZE + 4096
    img[primary_slot1 + 20] ^= 0xFF  # damage the primary copy: the backup copy is used
    assert lp.read_super(io.BytesIO(bytes(img)), 1).partition("system_b").size == len(SYSTEM)


def test_corruption_is_detected():
    img = bytearray(make_super({"system": SYSTEM}, 131072))
    img[lp.RESERVED + 8] ^= 0xFF
    img[lp.RESERVED + lp.GEOMETRY_SIZE + 8] ^= 0xFF
    with pytest.raises(lp.LpError, match="checksum"):
        lp.read_super(io.BytesIO(bytes(img)), 0)
    with pytest.raises(lp.LpError):
        lp.read_super(io.BytesIO(b"\0" * 65536), 0)


def test_empty_layout_matches_what_aera_accepts():
    md = lp.read_super(io.BytesIO(make_super({"system": SYSTEM, "my_region": REGION}, 131072)), 0)
    layout = lp.read_image(lp.write_image(lp.empty_layout(md, "_a")))
    groups = {g.name: g for g in layout.groups if g.name != "default"}
    assert set(groups) == {"qti_dynamic_partitions_a", "qti_dynamic_partitions_b"}
    assert all(g.flags == 0 for g in groups.values())
    assert groups["qti_dynamic_partitions_a"].maximum_size == groups["qti_dynamic_partitions_b"].maximum_size
    names = {p.name for p in layout.partitions}
    assert names == {"system_a", "system_b", "my_region_a", "my_region_b"}
    for p in layout.partitions:
        assert not p.extents and p.attributes & ~lp.ATTR_READONLY == 0
        assert layout.groups[p.group].name.endswith(p.name[-2:])
    assert len(lp.write_image(lp.empty_layout(md, "_a"))) < 4 * 1024 * 1024


# --- the restore zip ------------------------------------------------------------


@pytest.fixture
def lp_phone(phone):
    (phone / "by-name" / "super").write_bytes(make_super({"system": SYSTEM, "my_region": REGION}, 131072))
    return phone


def test_restore_zip_contents(adb, lp_phone, tmp_path):
    folder = backup_of(adb, tmp_path, "full")
    seen = []
    out = rz.build(folder, tmp_path / "out", progress=lambda w, d, t: seen.append((d, t)))
    assert "FAKE0001" in out.name and out.suffix == ".zip"
    assert seen[-1][0] == seen[-1][1]
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert "android-info.txt" in names and "super_empty.img" in names
        assert z.read("android-info.txt").decode() == "require board=testdevice|dodge\n"
        assert z.read("boot.img") == (lp_phone / "by-name" / "boot_a").read_bytes()
        assert z.read("system.img") == SYSTEM and z.read("my_region.img") == REGION
        for never in ("super.img", "persist.img", "userdata.img", "metadata.img", "modemst1.img", "boot_b.img"):
            assert never not in names
        layout = lp.read_image(z.read("super_empty.img"))
        assert {p.name for p in layout.partitions} == {"system_a", "system_b", "my_region_a", "my_region_b"}
        sums = dict(line.split("  ")[::-1] for line in z.read("SHA256SUMS").decode().splitlines())
        for name, digest in sums.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == digest
        meta = json.loads(z.read("opxflasher-restore.json"))
        assert meta["phone"]["serial"] == "FAKE0001" and meta["only_for_this_phone"]


def test_restore_zip_compressed(adb, lp_phone, tmp_path):
    out = rz.build(backup_of(adb, tmp_path, "full"), tmp_path / "out", compress=True)
    with zipfile.ZipFile(out) as z:
        assert z.getinfo("system.img").compress_type == zipfile.ZIP_DEFLATED
        assert z.read("system.img") == SYSTEM


def test_restore_zip_refuses_bad_super_and_cleans_up(adb, phone, tmp_path):
    folder = backup_of(adb, tmp_path, "full")  # the fake super isn't LP metadata
    with pytest.raises(rs.RestoreError, match="layout of super"):
        rz.build(folder, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_restore_zip_cancel_leaves_nothing(adb, lp_phone, tmp_path):
    folder = backup_of(adb, tmp_path, "full")
    with pytest.raises(rs.RestoreError, match="Cancelled"):
        rz.build(folder, tmp_path / "out", cancel=lambda: True)
    assert list((tmp_path / "out").iterdir()) == []


def test_restore_zip_needs_a_full_backup(adb, phone, tmp_path):
    with pytest.raises(rs.RestoreError, match="Full firmware"):
        rz.build(backup_of(adb, tmp_path, "critical"), tmp_path / "out")


def test_cli_restore_zip(adb, lp_phone, tmp_path, capsys):
    from opx import cli

    folder = backup_of(adb, tmp_path, "full")
    assert cli.main(["restore-zip", str(folder), "--out", str(tmp_path / "z")]) == 0
    assert "ONLY for the phone" in capsys.readouterr().out
    assert len(list((tmp_path / "z").glob("OPxFlasher-restore-*.zip"))) == 1
