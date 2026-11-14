"""Verify ROM: integrity, completeness, headers, AVB digests and what can use the ROM."""

import base64
import hashlib
import os
import zipfile

import pytest
import romfactory as rf
from test_convert import LOGICAL, make_ota

from opx import convert as cv
from opx.rom import verify as vf

MB = 1 << 20


def statuses(rep, area):
    return [f.status for f in rep.findings if f.area == area]


def target(rep, name):
    return next(t for t in rep.targets if t.name == name)


def boot_set():
    boot = rf.build_boot_image(b"ramdisk-1", size=3 * 4096)
    vendor_boot = b"VNDRBOOT" + os.urandom(4096 - 8)
    dtbo = b"\xd7\xb7\xab\x1e" + os.urandom(4096 - 4)
    vbmeta = rf.build_vbmeta(1, hashes=[("boot", boot), ("vendor_boot", vendor_boot), ("dtbo", dtbo)],
                             chains=[("vbmeta_system", 2)])
    return {"boot": boot, "vendor_boot": vendor_boot, "dtbo": dtbo, "vbmeta": vbmeta,
            "vbmeta_system": rf.build_vbmeta(1), "abl": rf.build_qcom_elf(3)}


def folder_rom(tmp_path, images=None):
    d = tmp_path / "rom"
    d.mkdir()
    for name, data in (images or {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}).items():
        (d / f"{name}.img").write_bytes(data)
    return d


def test_full_ota_verifies_every_block(tmp_path):
    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    seen = []
    rep = vf.verify(ota, progress=lambda what, done, total: seen.append(what))
    assert rep.verdict in ("VERIFIED", "VERIFIED WITH WARNINGS"), rep.lines()
    assert statuses(rep, "Integrity") == ["ok"] and "Every data block" in rep.findings[[f.area for f in rep.findings].index("Integrity")].text
    assert target(rep, "OPxFlasher").status == vf.YES
    assert target(rep, "OrangeFox / AERA").status == vf.CONVERT and rep.needs_conversion
    assert seen  # progress was reported


def test_damaged_payload_fails(tmp_path):
    parts = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    payload = rf.build_payload(parts, groups={"qti_dynamic_partitions": LOGICAL}, corrupt="system")
    ota = rf.build_ota_zip(tmp_path / "bad.zip", payload, rf.ota_metadata(codename="dodge"))
    rep = vf.verify(ota)
    assert rep.verdict == "FAILED" and "damaged" in " ".join(f.text for f in rep.findings if f.area == "Integrity")
    assert all(t.status == vf.NO for t in rep.targets)


def _ota_with_file_hash(tmp_path, good=True):
    parts = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    payload = rf.build_payload(parts, groups={"qti_dynamic_partitions": LOGICAL})
    digest = hashlib.sha256(payload if good else payload + b"x").digest()
    path = tmp_path / "ota.zip"
    with zipfile.ZipFile(path, "w") as zf:
        meta = rf.ota_metadata(codename="dodge")
        zf.writestr("META-INF/com/android/metadata", "".join(f"{k}={v}\n" for k, v in meta.items()))
        zf.writestr("payload_properties.txt", f"FILE_HASH={base64.b64encode(digest).decode()}\nFILE_SIZE={len(payload)}\n")
        info = zipfile.ZipInfo("payload.bin")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, payload)
    return path


def test_payload_checked_against_payload_properties(tmp_path):
    rep = vf.verify(_ota_with_file_hash(tmp_path))
    assert "matches its SHA-256" in next(f.text for f in rep.findings if f.area == "Integrity")
    (tmp_path / "bad").mkdir()
    bad = vf.verify(_ota_with_file_hash(tmp_path / "bad", good=False))
    assert bad.verdict == "FAILED" and "Download it again" in next(f.text for f in bad.findings if f.area == "Integrity")


def test_incremental_fails(tmp_path):
    ota, _ = make_ota(tmp_path, incremental=True)
    rep = vf.verify(ota)
    assert rep.verdict == "FAILED" and statuses(rep, "Package") == ["fail"]


def test_folder_avb_digests_and_headers(tmp_path):
    rom = folder_rom(tmp_path)
    rep = vf.verify(rom)
    assert statuses(rep, "AVB digests") == ["ok"] and statuses(rep, "Headers") == ["ok"], rep.lines()
    assert statuses(rep, "AVB") == ["ok"]  # vbmeta_system is there


def test_patched_or_damaged_boot_fails_its_vbmeta_digest(tmp_path):
    rom = folder_rom(tmp_path)
    data = bytearray((rom / "boot.img").read_bytes())
    data[5000] ^= 1
    (rom / "boot.img").write_bytes(bytes(data))
    rep = vf.verify(rom)
    assert rep.verdict == "FAILED" and "boot" in next(f.text for f in rep.findings if f.area == "AVB digests")


def test_wrong_file_under_a_boot_name_fails(tmp_path):
    imgs = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    imgs["vendor_boot"] = os.urandom(4096)  # not a vendor_boot image
    rep = vf.verify(folder_rom(tmp_path, imgs), deep=False)
    assert "vendor_boot" in next(f.text for f in rep.findings if f.area == "Headers" and f.status == "fail")


def test_missing_core_images_fail(tmp_path):
    imgs = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    del imgs["boot"]
    rep = vf.verify(folder_rom(tmp_path, imgs), deep=False)
    assert rep.verdict == "FAILED" and "boot" in next(f.text for f in rep.findings if f.area == "Completeness")


def test_vbmeta_partitions_missing_from_the_rom_is_a_warning(tmp_path):
    imgs = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    del imgs["vbmeta_system"]
    rep = vf.verify(folder_rom(tmp_path, imgs), deep=False)
    assert "vbmeta_system" in next(f.text for f in rep.findings if f.area == "AVB" and f.status == "warn")


def test_quick_mode_reads_no_data(tmp_path):
    ota, _ = make_ota(tmp_path)
    rep = vf.verify(ota, deep=False)
    assert "Quick check" in next(f.text for f in rep.findings if f.area == "Integrity")


def test_converted_full_rom_zip_is_ready_for_recoveries(tmp_path):
    from opx.rom.ota import open_rom

    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    made = cv.convert(open_rom(ota), tmp_path / "out", make_zip=True, make_folder=True)
    rep = vf.verify(made["zip"])
    assert rep.verdict != "FAILED", rep.lines()
    assert target(rep, "OrangeFox / AERA").status == vf.YES and not rep.needs_conversion
    assert "recorded SHA-256" in next(f.text for f in rep.findings if f.area == "Integrity")
    # a damaged image in the converted folder is caught
    folder = made["folder"]
    img = next(p for p in folder.rglob("system*.img"))
    raw = bytearray(img.read_bytes())
    raw[100] ^= 1
    img.write_bytes(bytes(raw))
    assert vf.verify(folder).verdict == "FAILED"


def test_not_a_rom(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    rep = vf.verify(f)
    assert rep.verdict == "FAILED" and all(t.status == vf.NO for t in rep.targets)


def test_cancel(tmp_path):
    ota, _ = make_ota(tmp_path)
    with pytest.raises(vf.VerifyCancelled):
        vf.verify(ota, cancel=lambda: True)


def test_damaged_sparse_image_fails(tmp_path):
    from test_convert import make_sparse

    imgs = {**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}}
    rom = folder_rom(tmp_path, imgs)
    good = make_sparse(os.urandom(4 * 4096))
    (rom / "system.img").write_bytes(good[:-100])  # truncated sparse image
    rep = vf.verify(rom)
    assert rep.verdict == "FAILED", rep.lines()


def test_sizes_read_well():
    assert (vf._size(9_270_000_000), vf._size(2_500_000), vf._size(4096)) == ("9.27 GB", "2.5 MB", "4 KB")


def test_cli_verify(tmp_path, capsys):
    import json

    from opx import cli

    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    assert cli.main(["rom", "verify", str(ota)]) == 0
    out = capsys.readouterr().out
    assert "Result: VERIFIED" in out and "OrangeFox / AERA" in out and "after converting" in out
    assert cli.main(["rom", "verify", str(ota), "--quick", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["deep"] is False and data["targets"][0]["name"] == "OPxFlasher"
    bad = tmp_path / "x.txt"
    bad.write_text("no")
    assert cli.main(["rom", "verify", str(bad)]) == 1


def verity_rom(tmp_path, damage=None):
    logical = {n: os.urandom(4096 * (3 + i)) for i, n in enumerate(LOGICAL)}
    imgs = {**boot_set(), **logical}
    imgs["vbmeta_system"] = rf.build_vbmeta(1, hashtrees=[(n, logical[n]) for n in ("system", "system_ext", "product")])
    # vendor carries its own vbmeta in an AVB footer, like real images do
    imgs["vendor"] = rf.with_footer(logical["vendor"], rf.build_vbmeta(1, hashtrees=[("vendor", logical["vendor"])]))
    if damage:
        raw = bytearray(imgs[damage])
        raw[4096 + 7] ^= 1
        imgs[damage] = bytes(raw)
    return folder_rom(tmp_path, imgs)


def test_dm_verity_hash_trees_of_loose_images(tmp_path):
    rep = vf.verify(verity_rom(tmp_path))
    assert statuses(rep, "dm-verity") == ["ok"], rep.lines()
    assert "4 system-type images" in next(f.text for f in rep.findings if f.area == "dm-verity")


@pytest.mark.parametrize("which", ["system", "vendor"])
def test_dm_verity_catches_one_flipped_bit(tmp_path, which):
    rep = vf.verify(verity_rom(tmp_path, damage=which))
    bad = next(f for f in rep.findings if f.area == "dm-verity")
    assert rep.verdict == "FAILED" and bad.status == "fail" and bad.text.startswith(which)
    (tmp_path / "quick").mkdir()
    quick = vf.verify(verity_rom(tmp_path / "quick", damage=which), deep=False)
    assert "dm-verity" not in [f.area for f in quick.findings]  # the quick check doesn't read the data


# Roots produced by AOSP avbtool (add_hashtree_footer --salt 73, sha256 / sha1) for
# data = bytes((i * 31 + blocks) % 251 for i in range(4096 * blocks)).
AVBTOOL_ROOTS = {
    ("sha256", 1): "6f4546aebd79cb75a23acbe790b570fe8a8edc650cfbd8d47978fc1dbc3ffa55",
    ("sha256", 2): "ad3e5daf6440d6478e32c84e2443a11df3c44f6961f43208c99a63e72c437470",
    ("sha256", 300): "5a21568acbe1a88ddc2f608421d2403892c7b3eddcdea6232c4bfa64fd8e7ff4",
    ("sha1", 1): "463b23451665904d558ae9fab8c7f7923a02ce8c",
    ("sha1", 2): "1e556245e5442a4ed03c50a7e3127728f690362c",
    ("sha1", 300): "8dc3597625e2724cac125af441038ae0ae58955a",
}


@pytest.mark.parametrize(("algo", "blocks"), sorted(AVBTOOL_ROOTS))
def test_hashtree_root_matches_avbtool(algo, blocks):
    from opx.rom import avb

    data = bytes((i * 31 + blocks) % 251 for i in range(4096 * blocks))
    d = avb.HashtreeDescriptor("system", len(data), 4096, 4096, algo, b"s", bytes.fromhex(AVBTOOL_ROOTS[algo, blocks]))
    pos = [0]

    def read(n):
        pos[0] += n
        return data[pos[0] - n : pos[0]]

    assert avb.hashtree_root(read, d) == d.root_digest
    if algo == "sha256":
        assert rf.verity_root(data, b"s") == d.root_digest  # the test factory builds them the same way


def test_hashtree_root_refuses_short_images():
    from opx.rom import avb

    with pytest.raises(avb.AvbError, match="shorter"):
        avb.hashtree_root(lambda n: b"", avb.HashtreeDescriptor("x", 8192, 4096, 4096, "sha256", b"", b""))
