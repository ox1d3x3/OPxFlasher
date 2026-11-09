import os

import pytest

import romfactory as rf
from opx.rom import avb, qcom
from opx.rom import payload as pl
from opx.rom.ota import RomError, open_rom

BOOT = os.urandom(3 * rf.BLOCK + 100)
VBMETA = rf.build_vbmeta(2, chains=[("vbmeta_system", 1)], hashes=["boot"], props={"com.android.build.boot.security_patch": "2026-09-01"})
SYSTEM = b"system" * 3000


def make_payload(**kw):
    parts = {"boot": BOOT, "vbmeta": VBMETA, "system": SYSTEM, "my_region": b"region" * 500}
    return rf.build_payload(
        parts,
        op_types={"boot": 8, "system": 1},
        groups={"qti_dynamic_partitions": ["system", "my_region"]},
        **kw,
    )


def test_payload_parse_and_extract(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(make_payload())
    payload = pl.parse(pl.ByteSource(p))
    assert payload.is_full
    assert [x.name for x in payload.partitions] == ["boot", "vbmeta", "system", "my_region"]
    assert payload.logical_names == {"system", "my_region"}
    assert payload.security_patch == "2026-09-01"
    for name, original in (("boot", BOOT), ("vbmeta", VBMETA), ("system", SYSTEM)):
        out = pl.extract(payload, name, tmp_path / f"{name}.img")
        assert out.read_bytes() == rf.pad(original)
    assert not list(tmp_path.glob("*.part"))


def test_corrupt_payload_is_rejected(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(make_payload(corrupt="boot"))
    payload = pl.parse(pl.ByteSource(p))
    with pytest.raises(pl.PayloadError, match="corrupt"):
        pl.extract(payload, "boot", tmp_path / "boot.img")
    assert not (tmp_path / "boot.img").exists()


def test_incremental_payload(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(make_payload(incremental={"system"}, minor_version=8))
    payload = pl.parse(pl.ByteSource(p))
    assert not payload.is_full
    with pytest.raises(pl.PayloadError, match="incremental"):
        pl.extract(payload, "system", tmp_path / "system.img")


def test_not_a_payload(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world" * 10)
    with pytest.raises(pl.PayloadError):
        pl.parse(pl.ByteSource(p))


def test_open_ota_zip(tmp_path):
    z = rf.build_ota_zip(tmp_path / "ota.zip", make_payload(), rf.ota_metadata(product="CPH2649"))
    rom = open_rom(z)
    assert rom.kind == "ota-zip"
    assert rom.codename == "testdevice" and rom.product == "CPH2649" and rom.android == "16"
    assert rom.is_full is True
    assert rom.read_image("vbmeta")[: len(VBMETA)] == VBMETA
    assert rom.image("system").logical is True and rom.image("boot").logical is False


def test_incremental_ota_zip(tmp_path):
    z = rf.build_ota_zip(tmp_path / "ota.zip", make_payload(), rf.ota_metadata(incremental=True))
    assert open_rom(z).is_full is False


def test_open_folder_rom(tmp_path):
    (tmp_path / "rom").mkdir()
    (tmp_path / "rom" / "boot.img").write_bytes(BOOT)
    (tmp_path / "rom" / "vbmeta.img").write_bytes(VBMETA)
    rom = open_rom(tmp_path / "rom")
    assert rom.kind == "folder" and {i.name for i in rom.images} == {"boot", "vbmeta"}
    assert rom.read_image("vbmeta") == VBMETA
    assert rom.codename == "" and rom.notes


def test_folder_with_payload(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "payload.bin").write_bytes(make_payload())
    assert open_rom(tmp_path / "x").kind == "payload"


def test_missing_rom(tmp_path):
    with pytest.raises(RomError):
        open_rom(tmp_path / "nope.zip")


def test_avb_parse():
    vb = avb.parse(VBMETA)
    assert vb.rollback_index == 2 and vb.rollback_index_location == 0
    assert vb.chains == [("vbmeta_system", 1)]
    assert vb.hash_partitions == ["boot"]
    assert vb.properties["com.android.build.boot.security_patch"] == "2026-09-01"
    assert vb.release.startswith("avbtool") and not vb.verification_disabled


def test_avb_footer_and_flags():
    image = rf.with_footer(b"\x11" * 8192, rf.build_vbmeta(5, location=3, flags=2))
    vb = avb.parse(image)
    assert vb.from_footer and vb.rollback_index == 5 and vb.rollback_index_location == 3
    assert vb.verification_disabled
    with pytest.raises(avb.AvbError):
        avb.parse(b"\0" * 4096)


def test_qcom_arb():
    arb = qcom.read_arb(rf.build_qcom_elf(arb=3, software_id=0x1C))
    assert arb.mbn_version == 6 and arb.value == 3 and arb.software_id == 0x1C
    legacy = qcom.read_arb(rf.build_qcom_elf(arb=1, mbn_version=3))
    assert legacy.value is None and "Legacy" in legacy.note
    with pytest.raises(qcom.QcomError):
        qcom.read_arb(b"not an elf at all" * 4)
