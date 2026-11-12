"""Dashboard facts, log export and the update check."""

import io
import json
import zipfile

import pytest

from opx import support as sp


def test_bootloader_text():
    assert sp.bootloader_text({"ro.boot.verifiedbootstate": "orange"}) == "Unlocked"
    assert sp.bootloader_text({"ro.boot.verifiedbootstate": "green"}) == "Locked"
    assert sp.bootloader_text({}) == "Unknown"


def manifest(folder, serial, mode="critical", created="2026-11-10T10:00:00+00:00", finished=True):
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(
        {"mode": mode, "created_utc": created, "finished": finished, "device": {"serial": serial}}))


def test_find_backups_for_this_phone_newest_first(tmp_path):
    manifest(tmp_path / "a", "S1", "critical", "2026-11-10T10:00:00+00:00")
    manifest(tmp_path / "b", "S1", "full", "2026-11-12T09:30:00+00:00")
    manifest(tmp_path / "c", "OTHER", "full", "2026-11-13T00:00:00+00:00")
    manifest(tmp_path / "d", "S1", "full", "2026-11-14T00:00:00+00:00", finished=False)
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "manifest.json").write_text("{not json")
    found = sp.find_backups(tmp_path, "S1")
    assert [b.folder.name for b in found] == ["d", "b", "a"]
    assert sp.last_backup_text(found) == "Full, 2026-11-12 09:30"  # unfinished one skipped
    assert sp.last_backup_text([]).startswith("None yet")
    assert sp.find_backups(tmp_path / "missing", "S1") == []


def test_mask_serials():
    assert sp.mask("phone 1a2b3c4d ok, again 1a2b3c4d", ["1a2b3c4d"]) == "phone 1a****4d ok, again 1a****4d"
    assert sp.mask("short ab", ["ab"]) == "short ab"


def test_export_log_hides_serials(tmp_path):
    j = tmp_path / "flash-1" / "journal.json"
    j.parent.mkdir()
    j.write_text('{"device": {"serial": "FAKE0001"}}')
    out = sp.export_log(tmp_path / "log.zip", "Connected FAKE0001\nflash boot_a", {"serial": "FAKE0001", "model": "PJZ110"},
                        ["FAKE0001"], [j])
    with zipfile.ZipFile(out) as z:
        text = "".join(z.read(n).decode() for n in z.namelist())
        assert "FAKE0001" not in text and "FA****01" in text
        about = json.loads(z.read("about.json"))
        assert about["phone"]["model"] == "PJZ110" and about["opxflasher"]
        assert "journals/flash-1-journal.json" in z.namelist()


@pytest.mark.parametrize("a,b", [
    ("0.7.0-rc.1", "0.7.0-rc.2"), ("0.7.0-rc.9", "0.7.0"), ("0.6.0", "0.7.0-rc.1"), ("v0.7.0", "0.7.1-rc.1"),
])
def test_version_order(a, b):
    assert sp.parse_version(a) < sp.parse_version(b)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


RELEASES = [
    {"tag_name": "v0.7.0-rc.6", "prerelease": True, "html_url": "https://x/rc6"},
    {"tag_name": "v0.7.0-rc.5", "prerelease": True, "html_url": "https://x/rc5"},
    {"tag_name": "v0.6.0", "prerelease": True, "html_url": "https://x/060"},
    {"tag_name": "v0.8.0", "draft": True, "prerelease": False},
    {"tag_name": "nightly", "prerelease": True},
]


def opener(data):
    def open_(req, timeout=None):
        assert req.full_url == sp.RELEASES_API and req.get_header("User-agent").startswith("OPxFlasher/")
        return Resp(json.dumps(data).encode())
    return open_


def test_update_check():
    up = sp.check_for_update("0.7.0-rc.5", opener=opener(RELEASES))
    assert up.tag == "v0.7.0-rc.6" and up.prerelease and up.url == "https://x/rc6"
    assert sp.check_for_update("0.7.0-rc.6", opener=opener(RELEASES)) is None
    assert sp.check_for_update("0.7.0-rc.5", include_candidates=False, opener=opener(RELEASES)) is None
    assert sp.check_for_update("0.7.0", opener=opener(RELEASES)) is None  # a final is newer than its rcs


def test_update_check_offline():
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("no network")
    with pytest.raises(OSError, match="Couldn't reach GitHub"):
        sp.check_for_update("0.7.0", opener=boom)
