"""Debloat, partition explorer and log saving against the fake phone."""

import json
import zipfile

import pytest
from test_flash import phone  # noqa: F401  (fixture)

from opx import apps as ap
from opx import bootlogs, cli
from opx import explorer as ex
from opx.remote import UnsafeCommand
from opx.session import open_session


@pytest.fixture
def undo(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "undo_dir", lambda: tmp_path / "debloat")
    return tmp_path / "debloat"


def pkgs(phone):
    return json.loads((phone / "packages.json").read_text())


# --- debloat ------------------------------------------------------------------------


def test_keep_rules():
    assert ap.keep_reason("com.android.systemui")
    assert ap.keep_reason("com.android.providers.telephony")
    assert not ap.keep_reason("com.heytap.browser")
    assert ap.keep_reason("com.oplus.telephony.ext", ["com.oplus.telephony*"])
    assert ap.keep_reason("com.oplus.battery", ["com.oplus.battery"])
    assert not ap.keep_reason("com.oplus.batteryx", ["com.oplus.battery"])
    for bad in ("com.x; reboot", "../etc", "com", "a.b c", "com.x$(id)"):
        with pytest.raises(ap.AppError):
            ap.check_package(bad)


def test_list_remove_restore(adb, phone, undo):
    s = open_session("FAKE0001", adb)
    apps = {a.package: a for a in ap.list_system_apps(s.adb)}
    assert apps["com.coloros.weather2"].disabled and not apps["com.heytap.browser"].removed
    assert apps["com.android.systemui"].keep and not apps["com.heytap.browser"].keep

    ap.remove(s.adb, s.serial, "com.heytap.browser")
    assert pkgs(phone)["com.heytap.browser"]["removed"]
    assert ap.removed_by_us(s.serial) == ["com.heytap.browser"]
    assert {a.package: a for a in ap.list_system_apps(s.adb)}["com.heytap.browser"].removed

    with pytest.raises(ap.AppError, match="needed by android"):
        ap.remove(s.adb, s.serial, "com.android.systemui")
    with pytest.raises(ap.AppError, match="not installed"):
        ap.remove(s.adb, s.serial, "com.heytap.browser")

    ap.restore(s.adb, s.serial, "com.heytap.browser")
    assert not pkgs(phone)["com.heytap.browser"]["removed"] and ap.removed_by_us(s.serial) == []
    with pytest.raises(ap.AppError, match="doesn't exist"):
        ap.restore(s.adb, s.serial, "com.not.there")


def test_debloat_list_files(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("# my list\ncom.heytap.browser  # browser\n\ncom.oplus.games\ncom.heytap.browser\n")
    assert ap.read_list(f) == ["com.heytap.browser", "com.oplus.games"]
    f.write_text("com.ok.app\nrm -rf /\n")
    with pytest.raises(ap.AppError):
        ap.read_list(f)
    out = ap.write_list(tmp_path / "out.txt", ["com.a.b"], "OnePlus 13")
    assert ap.read_list(out) == ["com.a.b"]


def test_cli_apps(adb_env, phone, undo, tmp_path, capsys):
    lst = tmp_path / "list.txt"
    lst.write_text("com.heytap.browser\ncom.oplus.games\ncom.android.systemui\ncom.not.here\n")
    assert cli.main(["apps", "remove", "--from", str(lst), "-y"]) == 0
    out = capsys.readouterr().out
    assert "kept (needed by android)" in out and "not a system app" in out
    assert "ok    com.heytap.browser" in out and "ok    com.oplus.games" in out
    assert pkgs(phone)["com.oplus.games"]["removed"] and not pkgs(phone)["com.android.systemui"]["removed"]

    assert cli.main(["apps", "list", "--removed"]) == 0
    out = capsys.readouterr().out
    assert "com.oplus.games" in out and "(by OPxFlasher)" in out and "systemui" not in out

    assert cli.main(["apps", "export", str(tmp_path / "mine.txt")]) == 0
    assert ap.read_list(tmp_path / "mine.txt") == ["com.heytap.browser", "com.oplus.games"]

    assert cli.main(["apps", "restore", "--all"]) == 0
    assert not any(v["removed"] for v in pkgs(phone).values())


def test_cli_apps_remove_asks(adb_env, phone, undo, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert cli.main(["apps", "remove", "com.oplus.games"]) == 1
    assert "Cancelled" in capsys.readouterr().out
    assert not (phone / "packages.json").exists()  # nothing changed


# --- partition explorer -------------------------------------------------------------


def test_explorer(adb, phone, monkeypatch):
    s = open_session("FAKE0001", adb)
    rows = {r.name: r for r in ex.explore(s.shell, s.profile, s.props)}
    assert rows["boot_a"].role == "Running slot" and rows["boot_b"].role == "Other slot"
    assert rows["boot_a"].size == (phone / "by-name" / "boot_a").stat().st_size
    assert "never flashed" in rows["persist"].role
    assert rows["userdata"].role.startswith("Your data")
    assert any(r.kind == "logical" and "inside super" in r.role for r in rows.values())
    csv = ex.to_csv(list(rows.values()))
    assert csv.startswith("name,size_bytes,kind,slot,role\n") and "boot_a," in csv


def test_explorer_needs_root(adb, phone, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "none")
    s = open_session("FAKE0001", adb)
    with pytest.raises(ex.ExplorerError, match="needs root"):
        ex.explore(s.shell, s.profile, s.props)


def test_cli_partitions_all(adb_env, phone, tmp_path, capsys):
    assert cli.main(["partitions", "--all"]) == 0
    assert "Running slot" in capsys.readouterr().out
    assert cli.main(["partitions", "--csv", str(tmp_path / "p.csv")]) == 0
    assert (tmp_path / "p.csv").read_text().count("\n") > 5


# --- logs ---------------------------------------------------------------------------


def test_logs_with_root(adb, phone, tmp_path):
    (phone / "pstore").mkdir()
    (phone / "pstore" / "console-ramoops-0").write_text("panic on FAKE0001\n")
    s = open_session("FAKE0001", adb)
    seen = []
    out = bootlogs.collect(s, tmp_path / "logs", emit=seen.append)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"getprop.txt", "logcat.txt", "dmesg.txt", "pstore/console-ramoops-0", "README.txt"} <= names
        for n in names:
            assert "FAKE0001" not in z.read(n).decode(), n  # serial hidden
        assert "FA****01" in z.read("pstore/console-ramoops-0").decode()
        assert "Kernel panic" not in z.read("findings.txt").decode()  # 'panic on' isn't a kernel panic line
    assert "Kernel log (dmesg)" in seen


def test_logs_without_root(adb, phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "none")
    s = open_session("FAKE0001", adb)
    out = bootlogs.collect(s, tmp_path)
    with zipfile.ZipFile(out) as z:
        assert "dmesg.txt" not in z.namelist() and "logcat.txt" in z.namelist()
        assert "need root" in z.read("README.txt").decode()


def test_log_commands_are_exact(adb, phone):
    s = open_session("FAKE0001", adb)
    with pytest.raises(UnsafeCommand):
        s.shell.read_log("pstore", "../../data/x")
    with pytest.raises(UnsafeCommand):
        s.shell.shell("cat /data/system/users/0.xml")
    with pytest.raises(UnsafeCommand):
        s.shell.shell("logcat -d; reboot")


def test_cli_logs(adb_env, phone, tmp_path, capsys):
    assert cli.main(["logs", "--out", str(tmp_path)]) == 0
    assert "Saved" in capsys.readouterr().out and list(tmp_path.glob("OPxFlasher-logs-*.zip"))
