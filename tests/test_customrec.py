"""Custom recovery (AERA): the owner's own download, its image found and checked, flashed to recovery."""

import hashlib
import zipfile

import pytest
import romfactory as rf
from test_flash_cli import answers, ready  # noqa: F401  (fixture)

from opx import cli
from opx import customrec as cr
from opx import profile as pf

REC = rf.build_boot_image(b"aera-recovery", header_version=4).ljust(2 * 1024 * 1024, b"\0")


def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_zip_image_is_found_by_what_it_is(tmp_path):
    z = make_zip(tmp_path / "AERA-R1.0-dodge.zip", {"README.txt": b"hi", "images/aera.img": REC,
                                                    "flash.bat": b"fastboot flash recovery aera.img"})
    lo = cr.load(z, tmp_path / "w")
    assert lo.from_zip and lo.member == "images/aera.img"
    assert lo.image.read_bytes() == REC and lo.image.name == cr.OUT_NAME
    assert lo.source_sha256 == hashlib.sha256(z.read_bytes()).hexdigest()
    assert lo.image_sha256 == hashlib.sha256(REC).hexdigest()


def test_several_images_prefer_the_recovery_one_or_stop(tmp_path):
    other = rf.build_boot_image(b"boot", header_version=4).ljust(2 * 1024 * 1024, b"\0")
    lo = cr.load(make_zip(tmp_path / "a.zip", {"boot.img": other, "recovery.img": REC}), tmp_path / "w")
    assert lo.member == "recovery.img"
    with pytest.raises(cr.CustomRecError, match="several images"):
        cr.load(make_zip(tmp_path / "b.zip", {"x.img": other, "y.img": REC}), tmp_path / "w")
    with pytest.raises(cr.CustomRecError, match="no recovery image"):
        cr.load(make_zip(tmp_path / "c.zip", {"notes.txt": b"x" * 2_000_000}), tmp_path / "w")


def test_img_and_wrong_files(tmp_path):
    p = tmp_path / "recovery.img"
    p.write_bytes(REC)
    lo = cr.load(p, tmp_path / "w")
    assert not lo.from_zip and lo.image == p
    bad = tmp_path / "vbmeta.img"
    bad.write_bytes(rf.build_vbmeta(2).ljust(2 * 1024 * 1024, b"\0"))
    with pytest.raises(cr.CustomRecError, match="isn't a recovery image"):
        cr.load(bad, tmp_path / "w")
    tiny = tmp_path / "tiny.img"
    tiny.write_bytes(REC[:4096])
    with pytest.raises(cr.CustomRecError, match="size"):
        cr.load(tiny, tmp_path / "w")


def test_sha_compare(tmp_path):
    p = tmp_path / "recovery.img"
    p.write_bytes(REC)
    lo = cr.load(p, tmp_path / "w")
    h = lo.source_sha256
    assert cr.matches(lo, "") is None and cr.matches(lo, "not a hash") is None
    assert cr.matches(lo, f"SHA-256: {h.upper()}") is True
    assert cr.matches(lo, " ".join(h[i:i + 8] for i in range(0, 64, 8))) is True  # pasted in groups
    assert cr.matches(lo, "0" * 64) is False


def test_oneplus13_links_to_aera_official_page():
    p = pf.load("oneplus-13")
    assert p.custom_recoveries == [{"name": "AERA", "url": "https://aera-recovery.com/devices/dodge"}]
    assert ("OnePlus 13", p.custom_recoveries[0]) in cr.all_pages()
    raw = {"id": "x", "name": "X", "custom_recoveries": [{"name": "Bad", "url": "http://insecure"}]}
    assert pf.load_raw_profile(raw).custom_recoveries == []  # https only


def test_cli_flash_recovery_from_the_zip(ready, capsys, monkeypatch, tmp_path):
    phone, state, _, backup = ready
    for slot in ("a", "b"):
        (phone / "by-name" / f"recovery_{slot}").write_bytes(b"\0" * (4 * 1024 * 1024))
    z = make_zip(tmp_path / "AERA.zip", {"recovery.img": REC})
    args = ["flash-image", "recovery", str(z), "--slot", "both", "--backup", str(backup), "--work", str(tmp_path / "w")]
    assert cli.main(args + ["--expect-sha256", "0" * 64]) == 2
    assert "Does NOT match" in capsys.readouterr().out and state.read()["log"] == []
    good = hashlib.sha256(z.read_bytes()).hexdigest()
    answers(monkeypatch, "FLASH")
    assert cli.main(args + ["--expect-sha256", good]) == 0
    out = capsys.readouterr().out
    assert "Matches" in out and "recovery.img" in out
    for slot in ("a", "b"):
        assert (phone / "by-name" / f"recovery_{slot}").read_bytes().startswith(REC)
