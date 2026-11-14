"""ROM detection and conversion to an OPxFlasher full ROM (zip + folder)."""

import hashlib
import json
import os
import shutil
import struct
import zipfile

import pytest
import romfactory as rf
from test_restore_zip import REGION, SYSTEM, make_super

from opx import convert as cv
from opx.rom import detect, sparse
from opx.rom.ota import RomError, materialize, open_rom

MB = 1 << 20
FP = "OnePlus/PJZ110/dodge:16/BP2A.250605.015/U.1:user/release-keys"


def images():
    return {
        "boot": os.urandom(3 * 4096), "vendor_boot": os.urandom(4096), "dtbo": os.urandom(4096),
        "xbl_config": rf.build_qcom_elf(3), "abl": rf.build_qcom_elf(3), "persist": b"P" * 4096,
        "vbmeta": rf.build_vbmeta(1), "vbmeta_system": rf.build_vbmeta(1),
        "system": os.urandom(MB * 5 // 2), "vendor": os.urandom(8192), "odm": os.urandom(4096),
        "product": os.urandom(4096), "system_ext": os.urandom(4096), "my_region": os.urandom(4096),
    }


LOGICAL = ["system", "vendor", "odm", "product", "system_ext", "my_region"]


def make_ota(tmp_path, parts=None, incremental=False, codename="dodge", product="PJZ110", ts="1790000000"):
    parts = parts or images()
    payload = rf.build_payload(parts, groups={"qti_dynamic_partitions": LOGICAL},
                               incremental={"boot"} if incremental else None)
    meta = rf.ota_metadata(codename=codename, product=product, incremental=incremental)
    meta["post-timestamp"] = ts
    return rf.build_ota_zip(tmp_path / "PJZ110_16.0.10.501(CN01).zip", payload, meta), parts


def padded(data):
    return rf.pad(data)


def make_sparse(raw: bytes, blk: int = 4096) -> bytes:
    """raw -> Android sparse: a raw chunk, a fill chunk (zeros) and a don't-care chunk."""
    assert len(raw) % blk == 0
    n = len(raw) // blk
    a, b = n // 2, n - n // 2
    chunks = [struct.pack("<HHII", sparse.RAW, 0, a, 12 + a * blk) + raw[: a * blk]]
    rest = raw[a * blk:]
    if rest.strip(b"\0"):
        chunks.append(struct.pack("<HHII", sparse.RAW, 0, b, 12 + b * blk) + rest)
    else:
        half = b // 2
        chunks.append(struct.pack("<HHII", sparse.FILL, 0, half, 16) + b"\0\0\0\0")
        chunks.append(struct.pack("<HHII", sparse.DONT_CARE, 0, b - half, 12))
    head = struct.pack("<IHHHHIIII", sparse.MAGIC, 1, 0, 28, 12, blk, n, len(chunks), 0)
    return head + b"".join(chunks)


# --- sparse ---------------------------------------------------------------------


def test_sparse_round_trip(tmp_path):
    raw = os.urandom(4096 * 3) + b"\0" * 4096 * 5
    src = tmp_path / "x.img"
    src.write_bytes(make_sparse(raw))
    assert sparse.is_sparse(src) and sparse.raw_size(src) == len(raw)
    out = tmp_path / "raw.img"
    with open(out, "wb") as fh:
        sparse.expand(src, fh)
    assert out.read_bytes() == raw
    bad = tmp_path / "bad.img"
    bad.write_bytes(make_sparse(raw)[:40])
    with pytest.raises(sparse.SparseError), open(tmp_path / "o", "wb") as fh:
        sparse.expand(bad, fh)


# --- detection ----------------------------------------------------------------------


def test_detect_full_ota(tmp_path):
    ota, _ = make_ota(tmp_path)
    d = detect.detect(ota)
    assert d.ok and d.full and d.label.startswith("Full OTA") and d.product == "PJZ110" and d.device == "dodge"
    assert set(d.logical) == set(LOGICAL) and "boot" in d.physical and d.profile == "OnePlus 13"
    assert d.skipped == ["persist"] and d.missing == [] and d.convertible[0] and d.built == "2026-09-21"
    text = "\n".join(d.lines())
    assert "Convert:   can be converted" in text and "Never written: persist" in text


def test_detect_incremental_and_rubbish(tmp_path):
    ota, _ = make_ota(tmp_path, incremental=True)
    d = detect.detect(ota)
    assert d.ok and d.full is False and d.label == "Incremental OTA zip"
    assert not d.convertible[0] and "incremental" in d.convertible[1]
    junk = tmp_path / "notes.txt"
    junk.write_text("hello")
    bad = detect.detect(junk)
    assert not bad.ok and "doesn't recognise" in bad.problem
    enc = tmp_path / "PJZ110_11_A.10.ofp"
    enc.write_bytes(b"\x00" * 64)
    assert "encrypted" in detect.detect(enc).problem
    ozip = tmp_path / "x.zip.bin"
    ozip.write_bytes(b"OPPOENCRYPT!" + b"\0" * 64)
    assert ".ozip" in detect.detect(ozip).problem
    img = tmp_path / "boot.img"
    img.write_bytes(b"ANDROID!" + b"\0" * 100)
    assert "single partition image" in detect.detect(img).problem


def test_detect_missing_core(tmp_path):
    parts = images()
    for n in ("vendor_boot", "odm"):
        del parts[n]
    payload = rf.build_payload(parts, groups={"g": [n for n in LOGICAL if n in parts]})
    z = rf.build_ota_zip(tmp_path / "ota.zip", payload, rf.ota_metadata(codename="dodge"))
    assert detect.detect(z).missing == ["vendor_boot", "odm"]


# --- conversion ------------------------------------------------------------------------


def test_convert_ota_to_zip_and_folder(tmp_path):
    ota, parts = make_ota(tmp_path)
    rom = open_rom(ota)
    stages = []
    made = cv.convert(rom, tmp_path / "out", make_zip=True, make_folder=True, chunk=MB, stage=stages.append)
    z, folder = made["zip"], made["folder"]
    assert z.name == "PJZ110_16.0.10.501(CN01)-fullrom.zip" and folder.name == "PJZ110_16.0.10.501(CN01)-fullrom"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [folder.name, z.name]  # nothing left over
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        assert {"META-INF/com/google/android/update-binary", "opx/rom.list", "opxflasher-rom.json",
                "SHA256SUMS", "images/boot.img", "images/system.img.000", "images/system.img.002"} <= names
        assert not any("persist" in n for n in names)
        assert zf.read("images/boot.img") == padded(parts["boot"])
        listing = zf.read("opx/rom.list").decode()
        assert "product PJZ110" in listing and "board dodge" in listing and "timestamp 1790000000" in listing
        assert "image logical system" in listing and "image physical boot" in listing
        order = [ln.split()[2] for ln in listing.splitlines() if ln.startswith("image physical")]
        assert order[-2:] == ["vbmeta_system", "vbmeta"]  # AVB last
        manifest = json.loads(zf.read("opxflasher-rom.json"))
        assert manifest["left_out"] == ["persist"] and manifest["metadata"]["post-timestamp"] == "1790000000"
    assert (folder / "system.img").read_bytes() == padded(parts["system"])
    assert any("Left out" in s for s in stages)

    # OPxFlasher reads both back with the original identity, and can get every image out
    for path in (z, folder):
        again = open_rom(path)
        assert again.kind == "fullrom" and again.codename == "dodge" and again.product == "PJZ110"
        assert again.metadata["post-timestamp"] == "1790000000" and again.is_full
        assert again.image("system").logical and not again.image("boot").logical
        out = materialize(again, "system", tmp_path / f"sys-{path.name}.img")
        assert out.read_bytes() == padded(parts["system"])
        d = detect.detect(path)
        assert d.label.startswith("OPxFlasher full ROM") and d.flashable


def test_damaged_fullrom_zip_is_caught(tmp_path):
    ota, _ = make_ota(tmp_path)
    z = cv.convert(open_rom(ota), tmp_path / "out", chunk=MB)["zip"]
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "images/boot.img":
                data = b"X" + data[1:]
            dst.writestr(info, data)
    with pytest.raises(RomError, match="SHA-256"):
        materialize(open_rom(bad), "boot", tmp_path / "boot.img")


def test_incremental_is_refused(tmp_path):
    ota, _ = make_ota(tmp_path, incremental=True)
    with pytest.raises(cv.ConvertError, match="incremental"):
        cv.convert(open_rom(ota), tmp_path / "out")
    assert not any((tmp_path / "out").iterdir())


def test_folder_without_identity_gets_folder_not_zip(tmp_path):
    src = tmp_path / "imgs"
    src.mkdir()
    for n in ("boot", "system", "vendor"):
        (src / f"{n}.img").write_bytes(os.urandom(4096))
    rom = open_rom(src)
    with pytest.raises(cv.ConvertError, match="doesn't say which phone"):
        cv.convert(rom, tmp_path / "out")
    made = cv.convert(rom, tmp_path / "out", make_zip=False, make_folder=True)
    assert (made["folder"] / "boot.img").exists()


def test_flasher_kit_zip_with_sparse_images(tmp_path):
    vb = rf.build_vbmeta(1, props={"com.android.build.system.fingerprint": FP,
                                   "com.android.build.system.security_patch": "2026-08-01"})
    system = os.urandom(4096 * 4) + b"\0" * 4096 * 4
    kit = tmp_path / "Regional Flasher X9U.zip"
    with zipfile.ZipFile(kit, "w") as zf:
        zf.writestr("kit/Regional_Flasher.bat", "@echo off")
        zf.writestr("kit/OTA_FILES_HERE/boot.img", b"B" * 4096)
        zf.writestr("kit/OTA_FILES_HERE/system.img", make_sparse(system))
        zf.writestr("kit/OTA_FILES_HERE/vbmeta_system.img", vb)
    rom = open_rom(kit)
    assert rom.image("system").sparse and rom.image("system").size == len(system)
    made = cv.convert(rom, tmp_path / "out", make_folder=True)
    assert (made["folder"] / "system.img").read_bytes() == system
    with zipfile.ZipFile(made["zip"]) as zf:
        listing = zf.read("opx/rom.list").decode()
        assert "image auto system" in listing and "image auto boot" in listing  # guessed: decided on the phone
        assert "patch 2026-08-01" in listing and "timestamp" not in listing


def test_super_image_set_is_split_into_logical_partitions(tmp_path):
    src = tmp_path / "kit"
    src.mkdir()
    (src / "boot.img").write_bytes(os.urandom(4096))
    (src / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props={"com.android.build.system.fingerprint": FP}))
    (src / "super.img").write_bytes(make_sparse(make_super({"system": SYSTEM, "my_region": REGION}, 512 * 1024)))
    # says which phone, but not how new: no recovery zip (it couldn't rule out a downgrade)
    with pytest.raises(cv.ConvertError, match="how new it is"):
        cv.convert(open_rom(src), tmp_path / "first")
    (src / "vbmeta_system.img").write_bytes(rf.build_vbmeta(1, props={
        "com.android.build.system.fingerprint": FP, "com.android.build.system.security_patch": "2026-08-01"}))
    rom = open_rom(src)
    assert rom.image("super").sparse
    made = cv.convert(rom, tmp_path / "out", make_folder=True)
    folder = made["folder"]
    assert (folder / "system.img").read_bytes() == SYSTEM and (folder / "my_region.img").read_bytes() == REGION
    assert not (folder / "super.img").exists() and not (folder / "super.raw.img").exists()
    with zipfile.ZipFile(made["zip"]) as zf:
        listing = zf.read("opx/rom.list").decode()
        assert "image logical system" in listing and "image logical my_region" in listing


def test_raw_super_in_folder_is_read_without_converting(tmp_path):
    src = tmp_path / "kit"
    src.mkdir()
    (src / "boot.img").write_bytes(os.urandom(4096))
    (src / "super.img").write_bytes(make_super({"system": SYSTEM, "my_region": REGION}, 512 * 1024))
    rom = open_rom(src)
    assert rom.image("system").logical and rom.image("system").in_super is not None
    assert materialize(rom, "my_region", tmp_path / "r.img").read_bytes() == REGION


def test_split_super_pieces_are_joined(tmp_path):
    full = make_super({"system": SYSTEM, "my_region": REGION}, 256 * 1024)
    half = len(full) // 2
    src = tmp_path / "kit"
    src.mkdir()
    for i, (lo, hi) in enumerate(((0, half), (half, len(full)))):
        piece = bytearray(len(full))
        piece[lo:hi] = full[lo:hi]
        # each piece: raw where it has data, don't-care elsewhere
        blk, n = 4096, len(full) // 4096
        chunks = []
        for b in range(n):
            seg = bytes(piece[b * blk:(b + 1) * blk])
            if lo <= b * blk < hi:
                chunks.append(struct.pack("<HHII", sparse.RAW, 0, 1, 12 + blk) + seg)
            else:
                chunks.append(struct.pack("<HHII", sparse.DONT_CARE, 0, 1, 12))
        head = struct.pack("<IHHHHIIII", sparse.MAGIC, 1, 0, 28, 12, blk, n, len(chunks), 0)
        (src / f"super.img.{i}").write_bytes(head + b"".join(chunks))
    rom = open_rom(src)
    assert rom.image("super").pieces and any("2 sparse pieces" in n for n in rom.notes)
    assert materialize(rom, "super", tmp_path / "s.img").read_bytes() == full


def test_existing_output_cancel_and_disk_space(tmp_path, monkeypatch):
    ota, _ = make_ota(tmp_path)
    out = tmp_path / "out"
    cv.convert(open_rom(ota), out, chunk=MB)
    with pytest.raises(cv.ConvertError, match="already exists"):
        cv.convert(open_rom(ota), out, chunk=MB)
    with pytest.raises(cv.ConvertError, match="Cancelled"):
        cv.convert(open_rom(ota), tmp_path / "out2", chunk=MB, cancel=lambda: True)
    assert list((tmp_path / "out2").iterdir()) == []
    monkeypatch.setattr(cv.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10**12, 10**12, 1000))
    with pytest.raises(cv.ConvertError, match="Not enough free space"):
        cv.convert(open_rom(ota), tmp_path / "out3", chunk=MB)


def test_fullrom_flashes_through_the_app_path(tmp_path):
    import threading

    from opx import flash

    ota, parts = make_ota(tmp_path)
    z = cv.convert(open_rom(ota), tmp_path / "out", chunk=MB)["zip"]
    rom = open_rom(z)
    files = flash.extract_images(rom, ["boot", "system"], tmp_path / "work", lambda e: None, threading.Event())
    assert files["system"].read_bytes() == padded(parts["system"])
    assert hashlib.sha256(files["boot"].read_bytes()).hexdigest() == rom.image("boot").sha256


def test_cli_detect_and_convert(tmp_path, capsys):
    from opx import cli

    ota, _ = make_ota(tmp_path)
    assert cli.main(["rom", "detect", str(ota)]) == 0
    out = capsys.readouterr().out
    assert "Full OTA zip" in out and "PJZ110" in out
    assert cli.main(["rom", "convert", str(ota), "--out", str(tmp_path / "out"), "--folder"]) == 0
    out = capsys.readouterr().out
    assert "-fullrom.zip" in out and "-fullrom" in out and "OrangeFox" in out
    assert cli.main(["rom", "convert", str(ota), "--out", str(tmp_path / "out")]) == 1  # exists already
    junk = tmp_path / "x.txt"
    junk.write_text("x")
    assert cli.main(["rom", "detect", str(junk)]) == 1


# --- full vs incremental: decided by the data -------------------------------------------


def _ota(tmp_path, name, meta_extra=None, **payload_kw):
    parts = images()
    payload = rf.build_payload(parts, groups={"qti_dynamic_partitions": LOGICAL}, **payload_kw)
    meta = rf.ota_metadata(codename="OP5D0DL1")
    meta.update(meta_extra or {})
    return rf.build_ota_zip(tmp_path / name, payload, meta)


@pytest.mark.parametrize("kw, meta, note", [
    ({"old_info": {n: b"" for n in ("boot", "system")}}, None, None),  # empty old_partition_info
    ({"old_info": {"boot": rf.f_int(1, 4096)}}, None, "name the version they replace"),
    ({"minor_version": 8}, None, "delta format (minor version 8)"),
    ({}, {"pre-build": "OnePlus/PJZ110/OP5D0DL1:16/X/U.0:user/release-keys"}, "names a build it updates from"),
])
def test_full_packages_with_misleading_labels(tmp_path, kw, meta, note):
    from opx import plan as pn

    z = _ota(tmp_path, "full.zip", meta, **kw)
    rom = open_rom(z)
    assert rom.is_full is True
    assert (note is None) == (not any("Full package:" in n for n in rom.notes))
    if note:
        assert any(note in n for n in rom.notes)
    d = detect.detect(z)
    assert d.full and d.convertible[0] and d.label.startswith("Full OTA")
    pkg = next(c for c in pn.build_report(rom, pn.no_device()).checks if c.name == "Package")
    assert pkg.status == pn.PASS
    assert cv.convert(rom, tmp_path / "out", chunk=MB)["zip"].exists()


def test_incremental_says_why(tmp_path):
    from opx import plan as pn

    z = _ota(tmp_path, "inc.zip", op_types={"system": 5}, incremental={"boot"})
    rom = open_rom(z)
    assert rom.is_full is False
    assert "2 of 14 partitions only hold changes" in rom.payload.incremental_reason
    assert "boot (SOURCE_COPY)" in rom.payload.incremental_reason
    assert "system (SOURCE_BSDIFF)" in rom.payload.incremental_reason
    pkg = next(c for c in pn.build_report(rom, pn.no_device()).checks if c.name == "Package")
    assert pkg.status == pn.BLOCK and "boot (SOURCE_COPY)" in pkg.detail
    assert any("boot (SOURCE_COPY)" in n for n in rom.notes)


def test_existing_outputs_are_verified_and_reused_or_replaced(tmp_path):
    from test_verify import boot_set

    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    out = tmp_path / "out"
    first = cv.convert(open_rom(ota), out, make_zip=True, make_folder=True)
    with pytest.raises(cv.ConvertError, match="already exists"):
        cv.convert(open_rom(ota), out, make_zip=True)
    assert cv.existing_outputs(open_rom(ota), out, True, True) == first
    stages = []
    again = cv.convert_or_reuse(open_rom(ota), out, make_zip=True, make_folder=True, stage=stages.append)
    assert again == first and any("Already converted" in s for s in stages)
    # only the missing one is made
    first["zip"].unlink()
    made = cv.convert_or_reuse(open_rom(ota), out, make_zip=True, make_folder=True)
    assert made["zip"].exists() and made["folder"] == first["folder"]
    # a damaged existing output is never reused
    img = next(first["folder"].rglob("system*.img"))
    raw = bytearray(img.read_bytes())
    raw[10] ^= 1
    img.write_bytes(bytes(raw))
    with pytest.raises(cv.ConvertError, match="failed verification"):
        cv.convert_or_reuse(open_rom(ota), out, make_folder=True, make_zip=False)
    fixed = cv.convert(open_rom(ota), out, make_zip=False, make_folder=True, replace=True)
    cv.verify_existing(fixed)
