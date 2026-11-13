import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402


def settle(app, cycles=200):
    for _ in range(cycles):
        QThreadPool.globalInstance().waitForDone(50)
        app.processEvents()


@pytest.fixture
def window(adb_env, monkeypatch):
    from opx.gui.app import MainWindow

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.poll_timer.setInterval(10**9)  # no background checks unless a test asks for one
    win.refresh_devices()
    settle(app)
    yield app, win
    # finish background jobs and free the window while Python is still running
    QThreadPool.globalInstance().waitForDone(10000)
    win.close()
    win.deleteLater()
    settle(app, 5)


def test_gui_connects_and_backs_up(window, tmp_path):
    app, win = window
    assert not win.windowIcon().isNull()
    assert win.device_combo.count() == 1
    assert win.session is not None and win.session.access.kind == "su"
    assert win.info_labels["model"].text() == "PJZ110"
    assert win.start_btn.isEnabled()

    # choose "Network essentials" and back up
    win.mode_group.buttons()[0].setChecked(True)
    win.out_edit.setText(str(tmp_path / "out"))
    win.start_backup()
    settle(app)

    assert win.last_folder is not None
    manifest = json.loads((win.last_folder / "manifest.json").read_text())
    assert [p["status"] for p in manifest["partitions"]] == ["ok", "ok", "ok"]
    assert win.table.rowCount() == 3
    assert all(win.table.item(r, 3).text() == "verified" for r in range(3))
    assert not win.busy


def test_gui_warns_without_root(adb_env, monkeypatch):
    monkeypatch.setenv("FAKE_ACCESS", "none")
    from opx.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.refresh_devices()
    settle(app)
    assert win.session.access.kind == "none"
    assert not win.start_btn.isEnabled()
    assert "root" in win.banner.text().lower()


def test_gui_rom_check_tab(window, adb_env, tmp_path):
    import romfactory as rf
    from test_plan import put, rom_zip, sized

    app, win = window
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
    rom = rom_zip(tmp_path)
    win.tabs.setCurrentIndex(1)
    win.rom_edit.setText(str(rom.path))
    win.check_rom()
    settle(app)
    assert win.report is not None
    assert win.report.verdict == "BLOCKED"  # Qualcomm ARB reader not validated yet
    table = win.checks_table
    checks = {table.item(r, 1).text(): table.item(r, 0).text() for r in range(table.rowCount())}
    assert checks["Device match"] == "OK" and checks["AVB vbmeta"] == "OK"
    assert win.plan_table.rowCount() == len(win.report.items)
    assert "BLOCKED" in win.verdict.text()
    log = win.log.toPlainText()  # the reasons are in the log too, for exported logs
    assert "ROM check: BLOCKED" in log and "BLOCK Qualcomm ARB" in log


def test_gui_flash_flow(adb_env, fb_state, monkeypatch, tmp_path):
    import sys

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_flash import make_rom
    from test_plan import put, sized

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(adb_env, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(adb_env, f"dtbo{slot}", b"\0" * 65536)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    from opx.gui import app as gui_app

    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    win.refresh_devices()
    settle(app)
    win.out_edit.setText(str(tmp_path / "out"))
    win.start_backup()  # critical backup first
    settle(app)
    assert win.last_folder is not None

    rom = make_rom(tmp_path)
    win.rom_edit.setText(str(rom.path))
    win.check_rom()
    settle(app)
    assert win.report.verdict == "BLOCKED"  # only the unvalidated ARB reader
    assert win.flash_btn.isEnabled()

    seen = {}

    class AutoDialog(gui_app.FlashDialog):
        def exec(self):
            assert not self.start_btn.isEnabled()  # nothing typed yet
            self.confirm_edit.setText("FLASH")
            assert not self.start_btn.isEnabled()  # ARB confirmation still missing
            self.arb_edit.setText("ARB CHECKED")
            seen["enabled"] = self.start_btn.isEnabled()
            seen["preview"] = self.preview.toPlainText()
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui_app, "FlashDialog", AutoDialog)
    win.start_flash()
    settle(app, cycles=600)

    assert seen["enabled"] and "All safety gates passed" in seen["preview"]
    assert win.flash_label.text() == "Flash complete"
    assert fb_state.read()["log"][-1] == "reboot"
    assert (adb_env / "by-name" / "dtbo_a").read_bytes().startswith(b"D" * 4096)


def _gui_phone(adb_env, monkeypatch):
    import sys

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_plan import put, sized

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(adb_env, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(adb_env, f"dtbo{slot}", b"\0" * 65536)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)


def test_gui_signal_restore(adb_env, fb_state, monkeypatch, tmp_path):
    from test_region import simulate_ota_overwrite

    from opx.gui import app as gui_app

    _gui_phone(adb_env, monkeypatch)
    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    win.refresh_devices()
    settle(app)
    win.out_edit.setText(str(tmp_path / "out"))
    win.start_backup()
    settle(app)
    original = (adb_env / "mapper" / "my_region_a").read_bytes()

    assert win.sig_backup_edit.text() == str(win.last_folder)  # latest backup offered automatically
    win.refresh_signal()
    hashes = {win.sig_table.item(r, 0).text(): win.sig_table.item(r, 3).text() for r in range(win.sig_table.rowCount())}
    assert hashes == {"oplusstanvbk_a": "OK", "oplusstanvbk_b": "OK", "my_region_a": "OK"}
    assert win.sig_plan.ok and not win.restore_btn.isEnabled()

    simulate_ota_overwrite(adb_env)
    win.sig_confirm.setText("RESTORE")
    assert win.restore_btn.isEnabled()
    win.start_restore()
    settle(app, cycles=600)
    assert win.sig_label.text() == "Restore complete"
    assert (adb_env / "mapper" / "my_region_a").read_bytes() == original
    assert fb_state.read()["log"][-1] == "reboot"


def test_flash_dialog_conversion_mode(adb_env, fb_state, monkeypatch, tmp_path):
    from test_region import rom_for

    from opx import backup as bk
    from opx import plan as pn
    from opx.gui.flash_dialog import FlashDialog
    from opx.session import open_session

    _gui_phone(adb_env, monkeypatch)
    _app = QApplication.instance() or QApplication([])  # needed before any widget
    s = open_session("FAKE0001")
    folder = bk.run_backup(s.shell, bk.build_plan(s.shell, s.profile, "critical", s.props), tmp_path / "b")
    rom = rom_for(tmp_path)
    device = pn.device_from_session(s)
    d = FlashDialog(None, rom, device, pn.build_report(rom, device), str(folder))
    assert d.keep_region_box.isChecked()
    assert d.wipe_box.isChecked() and not d.wipe_box.isEnabled()
    assert not d.optional_boxes["oplusstanvbk"].isEnabled()
    assert d.options().restore_from_backup == ["oplusstanvbk", "my_region"]
    d.arb_edit.setText("ARB CHECKED")
    d.confirm_edit.setText("FLASH")
    assert d.variant_box is not None and not d.variant_box.isChecked()  # default: no
    assert not d.plan.ok and any("Different variant" in p for p in d.plan.problems)
    assert not d.start_btn.isEnabled()
    d.variant_box.setChecked(True)
    assert d.plan.ok, d.plan.problems
    assert "Restore my_region from your backup" in d.preview.toPlainText()

    d.keep_region_box.setChecked(False)
    assert d.optional_boxes["oplusstanvbk"].isEnabled()
    assert d.options().restore_from_backup == []


def test_gui_root_flow(adb_env, fb_state, monkeypatch, tmp_path):
    from test_root import STOCK, magisk_patch, rom_with_init_boot
    from test_root_cli import make_rootable

    from opx.gui import app as gui_app

    _gui_phone(adb_env, monkeypatch)
    make_rootable(adb_env)
    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    win.refresh_devices()
    settle(app)
    win.out_edit.setText(str(tmp_path / "out"))
    win.start_backup()
    settle(app)
    tab = win.root_tab
    assert tab.backup_edit.text() == str(win.last_folder)
    assert not tab.pull_btn.isEnabled() and not tab.root_btn.isEnabled()

    tab.rom_edit.setText(str(rom_with_init_boot(tmp_path).path))
    tab.prepare()
    settle(app)
    assert tab.session is not None and "matches build" in tab.prepare_status.text()
    assert "Select and Patch a File" in tab.hint.text()

    tab.pull()  # not patched yet
    settle(app)
    assert "No new patched image" in tab.pull_status.text()

    patched = magisk_patch(adb_env)
    tab.pull()
    settle(app)
    assert tab.pull_problems == []
    tab.confirm.setText("UNROOT")
    assert not tab.root_btn.isEnabled() and tab.unroot_btn.isEnabled()
    tab.confirm.setText("ROOT")
    assert tab.root_btn.isEnabled()
    tab.flash("root")
    settle(app, cycles=600)
    assert tab.label.text() == "Root complete"
    assert (adb_env / "by-name" / "init_boot_a").read_bytes().startswith(patched)
    assert (adb_env / "by-name" / "init_boot_b").read_bytes().startswith(STOCK)


def test_gui_offers_to_download_adb_and_fastboot(adb_env, monkeypatch, tmp_path):
    from opx import device as dev
    from opx.gui import app as gui
    from opx.gui.app import MainWindow

    missing = dev.Scan(errors=["adb not found. Run 'opx tools fetch' to download Google's platform-tools"])
    monkeypatch.setattr(gui.dev, "scan", lambda: missing)
    _answer(monkeypatch, gui, "Download")
    fetched = []
    monkeypatch.setattr(gui, "fetch_platform_tools", lambda progress=None: fetched.append(1) or tmp_path / "pt")

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.refresh_devices()
    settle(app)
    assert win.tools_btn.isVisibleTo(win)
    assert "Tools not downloaded" in win.banner.text() and "Click here to download" in win.banner.text()

    monkeypatch.setattr(gui.dev, "scan", lambda: dev.Scan())  # tools now present, no phone
    win.tools_btn.click()
    settle(app)
    assert fetched == [1]
    assert not win.tools_btn.isVisibleTo(win)


def test_smoke_test_flag(monkeypatch, capsys):
    from opx.gui import app as gui

    monkeypatch.setattr(gui.sys, "argv", ["OPxFlasher", "--smoke-test"])
    QApplication.instance() or QApplication([])
    assert gui.main() == 0
    assert "smoke test ok" in capsys.readouterr().out


def test_gui_official_roms_dialog(window, monkeypatch, tmp_path):
    from test_catalog import PKG, SAMPLE

    from opx import catalog
    from opx.gui import catalog_dialog as cd

    app, win = window
    releases = [catalog.Release.from_json(r) for r in SAMPLE["releases"]]
    monkeypatch.setattr(cd.catalog, "for_phone", lambda model: [r for r in releases if r.model == model])
    monkeypatch.setattr(cd.catalog, "fetch", lambda **kw: releases)
    monkeypatch.setattr(cd.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))

    def fake_download(rel, folder, progress=None, cancel=None, stage=None, android="", build=""):
        stage("Asking the official server for the package...")
        progress(len(PKG), len(PKG))
        out = folder / rel.filename
        out.write_bytes(PKG)
        return out

    monkeypatch.setattr(cd.catalog, "download", fake_download)

    dlg = cd.CatalogDialog(win, win._dialog_job, "PJZ110")
    settle(app)
    assert dlg.table.rowCount() == 2
    assert dlg.selected().id == "bbb222"  # newest first

    dlg.others.setChecked(True)
    settle(app)
    assert dlg.table.rowCount() == 3
    eu = next(i for i, r in enumerate(dlg.releases) if r.model == "CPH2653")
    assert "other variant" in dlg.table.item(eu, 6).text()

    # another variant asks first; cancelling downloads nothing
    asked = []
    monkeypatch.setattr(cd.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or cd.QMessageBox.StandardButton.Cancel)
    dlg.table.selectRow(eu)
    dlg.download()
    settle(app)
    assert asked and "CPH2653" in asked[0] and dlg.downloaded is None

    dlg.table.selectRow(0)
    dlg.download()
    settle(app)
    assert dlg.downloaded == tmp_path / releases[1].filename
    assert dlg.result() == cd.QDialog.DialogCode.Accepted


def test_gui_flash_image_tab(window, monkeypatch, tmp_path, fb_state):
    import os
    import sys
    from pathlib import Path

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd

    from opx import cli

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    assert cli.main(["backup", "--mode", "critical", "--out", str(tmp_path / "b")]) == 0
    backup = next((tmp_path / "b").iterdir())
    boot = rf.build_boot_image(b"gui-kernel", header_version=4)
    (tmp_path / "boot.img").write_bytes(boot)

    tab = win.image_tab
    tab.part_combo.setCurrentText("boot")
    tab.image_edit.setText(str(tmp_path / "boot.img"))
    tab.backup_edit.setText(str(backup))
    assert not tab.verity.isEnabled()  # vbmeta only
    tab.check()
    assert tab.plan is not None and tab.plan.ok, tab.summary.text()
    assert not tab.flash_btn.isEnabled()
    tab.confirm.setText("FLASH")
    assert tab.flash_btn.isEnabled()
    tab.flash_btn.click()
    settle(app, 400)
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(boot), win.log.toPlainText()[-800:]

    # a wrong image type is stopped at Check
    (tmp_path / "vb.img").write_bytes(rf.build_vbmeta(2))
    tab.image_edit.setText(str(tmp_path / "vb.img"))
    tab.check()
    assert not tab.plan.ok and "not a boot image" in tab.summary.text()


def test_gui_reboot_buttons(window, monkeypatch, fb_state):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    btns = win.reboot_btns
    assert not btns["system"].isEnabled()  # already in Android
    assert btns["bootloader"].isEnabled() and btns["recovery"].isEnabled()
    btns["bootloader"].click()
    settle(app)
    assert fb_state.read()["mode"] == "bootloader"


def test_gui_restore_full_rom(window, monkeypatch, tmp_path, fb_state):
    import os
    import sys
    from pathlib import Path

    from conftest import FAKE_FASTBOOT, tool_cmd

    from opx import cli
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    assert cli.main(["backup", "--mode", "full", "--out", str(tmp_path / "b")]) == 0
    folder = next((tmp_path / "b").iterdir())
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    good = (phone / "by-name" / "boot_a").read_bytes()
    (phone / "by-name" / "boot_a").write_bytes(b"\0" * len(good))

    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    seen = {}

    def accept(dialog):
        seen["title"] = dialog.windowTitle()
        seen["preview"] = dialog.preview.toPlainText()
        dialog.confirm_edit.setText("FLASH")
        return gui.QDialog.DialogCode.Accepted if dialog.plan.ok else gui.QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui.FlashDialog, "exec", accept)
    win.restore_full_rom()
    settle(app, 400)
    assert seen["title"] == "Restore full ROM" and "Flash super.img" in seen["preview"]
    assert (phone / "by-name" / "boot_a").read_bytes() == good, win.log.toPlainText()[-600:]


def test_gui_make_recovery_zip(window, monkeypatch, tmp_path):
    import os
    from pathlib import Path

    from test_restore_zip import REGION, SYSTEM, make_super

    from opx import cli
    from opx.gui import app as gui

    app, win = window
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    (phone / "by-name" / "super").write_bytes(make_super({"system": SYSTEM, "my_region": REGION}, 131072))
    assert cli.main(["backup", "--mode", "full", "--out", str(tmp_path / "b")]) == 0
    folder = next((tmp_path / "b").iterdir())
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(win, "ask_recovery", lambda: None)
    win.make_restore_zip()  # cancelled at the question
    settle(app, 50)
    assert not shown
    monkeypatch.setattr(win, "ask_recovery", lambda: "orangefox")
    win.make_restore_zip()
    settle(app, 200)
    assert shown and "ONLY for the phone" in shown[0] and "OrangeFox / TWRP" in shown[0]
    assert len(list((tmp_path / "b").glob("OPxFlasher-restore-*-orangefox.zip"))) == 1
    win.make_restore_zip("aera")
    settle(app, 200)
    assert "AERA Recovery" in shown[-1]


def test_gui_language_picker(window, monkeypatch, tmp_path):
    from opx import settings
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setattr(settings, "path", lambda: tmp_path / "settings.json")
    assert list(win.lang_actions) == ["en"] and win.lang_actions["en"].isChecked()
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    win.change_language("de", "Deutsch")
    assert settings.get("language") == "de" and "Restart" in shown[0]

def test_gui_dashboard_export_and_updates(window, monkeypatch, tmp_path):
    import zipfile

    from opx import cli, support
    from opx.gui import app as gui

    app, win = window
    labels = win.info_labels
    assert labels["variant"].text() == "OnePlus 13 China (PJZ110)"
    assert labels["bootloader"].text() in ("Unlocked", "Locked", "Unknown")
    assert labels["last_backup"].text().startswith("None yet")

    win.out_edit.setText(str(tmp_path / "b"))
    assert cli.main(["backup", "--mode", "critical", "--out", str(tmp_path / "b")]) == 0
    win._refresh_last_backup()
    assert labels["last_backup"].text().startswith("Critical, ")

    out = tmp_path / "log.zip"
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.say("talking to FAKE0001")
    win.export_log()
    with zipfile.ZipFile(out) as z:
        assert "FAKE0001" not in z.read("log.txt").decode()

    monkeypatch.setattr(gui.support, "check_for_update",
                        lambda: support.Update("v9.0.0", "https://example.invalid/r", False))
    asked = []
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or gui.QMessageBox.StandardButton.No)
    win.check_updates()
    settle(app)
    assert asked and "v9.0.0" in asked[0]


def test_gui_apps_tab(window, monkeypatch, tmp_path):
    import json
    import os
    from pathlib import Path

    from opx import apps as ap
    from opx.gui import apps_tab

    app, win = window
    monkeypatch.setattr(ap, "undo_dir", lambda: tmp_path / "debloat")
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    tab = win.apps_tab
    assert tab.load_btn.isEnabled() and not tab.remove_btn.isEnabled()
    tab.load()
    settle(app)
    names = [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())]
    assert "com.heytap.browser" in names and "com.android.systemui" in names

    tab.search.setText("systemui")  # locked: can't be removed
    tab.table.selectRow(0)
    assert "Locked" in tab.table.item(0, 1).text() and not tab.remove_btn.isEnabled()

    tab.search.setText("heytap")
    tab.table.selectRow(0)
    assert tab.remove_btn.isEnabled()
    monkeypatch.setattr(apps_tab.QMessageBox, "question", lambda *a, **k: apps_tab.QMessageBox.Yes)
    tab.remove_selected()
    settle(app)
    assert json.loads((phone / "packages.json").read_text())["com.heytap.browser"]["removed"]
    assert "by OPxFlasher" in tab.table.item(0, 1).text()
    assert tab.restore_all_btn.isEnabled()

    out = tmp_path / "mine.txt"
    monkeypatch.setattr(apps_tab.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    tab.export_list()
    assert ap.read_list(out) == ["com.heytap.browser"]

    tab.restore_all()
    settle(app)
    assert not json.loads((phone / "packages.json").read_text())["com.heytap.browser"]["removed"]

    lst = tmp_path / "list.txt"
    lst.write_text("com.oplus.games\ncom.android.settings\ncom.not.here\n")
    monkeypatch.setattr(apps_tab.QFileDialog, "getOpenFileName", lambda *a, **k: (str(lst), ""))
    tab.remove_from_list()
    settle(app)
    state = json.loads((phone / "packages.json").read_text())
    assert state["com.oplus.games"]["removed"] and not state["com.android.settings"]["removed"]


def test_gui_diagnostics_tab(window, monkeypatch, tmp_path):
    import zipfile

    from opx.gui import diag_tab

    app, win = window
    tab = win.diag_tab
    assert tab.logs_btn.isEnabled() and tab.list_btn.isEnabled() and not tab.csv_btn.isEnabled()
    tab.list_partitions()
    settle(app)
    names = [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())]
    assert "boot_a" in names and "partitions" in tab.summary.text()
    out = tmp_path / "p.csv"
    monkeypatch.setattr(diag_tab.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    tab.save_csv()
    assert out.read_text().startswith("name,size_bytes")

    shown = []
    monkeypatch.setattr(diag_tab.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    tab.save_logs(folder=str(tmp_path / "logs"))
    settle(app)
    zips = list((tmp_path / "logs").glob("OPxFlasher-logs-*.zip"))
    assert len(zips) == 1 and shown and "hidden" in shown[0]
    assert "No known problem" in shown[0]

    import zipfile as zf_

    bad = tmp_path / "bad.zip"
    with zf_.ZipFile(bad, "w") as zf:
        zf.writestr("dmesg.txt", "Kernel panic - not syncing\n")
    warned = []
    monkeypatch.setattr(diag_tab.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    tab.read_logs(path=str(bad))
    assert warned and "kernel crashed" in warned[0] and tab.last_findings[0].id == "panic"
    with zipfile.ZipFile(zips[0]) as z:
        assert "logcat.txt" in z.namelist()


def test_gui_rescue_dialog(window, monkeypatch, fb_state):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_sahara import FakeBootRom

    from opx import rescue, sahara
    from opx.gui import rescue_dialog

    app, win = window
    monkeypatch.setattr(sahara, "find_ports", lambda: [])
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    fb_state(mode="bootloader")
    win.open_rescue()
    settle(app)
    dlg = win.rescue_dialog
    assert dlg.assessment.state == "bootloader"
    assert "reboot:system" in dlg.buttons and "official-roms" in dlg.buttons

    # a phone in 9008: identify shows its details
    monkeypatch.setattr(rescue, "gather", lambda roots: rescue.assess([], True, False))
    dlg.check()
    settle(app)
    assert dlg.assessment.state == "edl" and "identify-9008" in dlg.buttons
    shown = []
    monkeypatch.setattr(rescue_dialog.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(sahara, "identify_first", lambda: sahara.identify(FakeBootRom(), "COM5"))
    dlg.buttons["identify-9008"].click()
    settle(app)
    assert shown and "Chip serial: 0x1a2b3c4d" in shown[0]
    dlg.close()


def test_gui_usb_driver_help(window, monkeypatch):
    from opx import usbcheck
    from opx.gui import app as gui

    app, win = window
    finding = usbcheck.check_windows([{"Status": "Error", "Class": None, "FriendlyName": "PJZ110",
                                       "InstanceId": "USB\\VID_22D9&PID_2769\\X"}])
    monkeypatch.setattr(usbcheck, "check", lambda: finding)
    clicks = iter(["Get the driver", None])
    seen = []

    def fake_exec(box):  # the user clicks Get the driver, then Close
        label = next(clicks)
        seen.append([b.text() for b in box.buttons()])
        box._clicked = next((b for b in box.buttons() if b.text() == label), None)

    monkeypatch.setattr(gui.QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(gui.QMessageBox, "clickedButton", lambda box: box._clicked)
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    win.usb_check()
    settle(app)
    assert opened == [usbcheck.GOOGLE_DRIVER]
    assert "Get the driver" in seen[0] and not any("Device Manager" in b for b in seen[0])



def test_gui_startup_tools_found_goes_straight_on(window, monkeypatch):
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setattr(gui.QMessageBox, "exec", lambda box: pytest.fail("no prompt expected"))
    refreshed = []
    monkeypatch.setattr(win, "refresh_devices", lambda: refreshed.append(True))
    win.check_tools()
    assert refreshed and win.banner.isHidden()
    assert "adb and fastboot found." in win.log.toPlainText()


def _answer(monkeypatch, gui, label):
    """Make the tools prompt click `label` ('Download' or 'Decline')."""
    def fake_exec(box):
        box._clicked = next(b for b in box.buttons() if b.text() == label)
    monkeypatch.setattr(gui.QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(gui.QMessageBox, "clickedButton", lambda box: box._clicked)


def test_gui_startup_tools_missing_decline_then_link(window, monkeypatch, tmp_path):
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setattr(gui, "missing_tools", lambda: ["adb", "fastboot"])
    _answer(monkeypatch, gui, "Decline")
    win.check_tools()
    assert not win.banner.isHidden() and "Tools not downloaded" in win.banner.text()
    assert "Click here to download" in win.banner.text() and win.tools_btn.isVisibleTo(win)

    # the banner link downloads straight away (no second question), with progress
    def fake_fetch(progress=None):
        progress(5, 10)
        progress(10, 10)
        return tmp_path / "platform-tools"
    monkeypatch.setattr(gui, "fetch_platform_tools", fake_fetch)
    refreshed = []
    monkeypatch.setattr(win, "refresh_devices", lambda: refreshed.append(True))
    win.banner.linkActivated.emit(win.TOOLS_LINK)
    settle(app)
    assert refreshed and win.banner.isHidden() and win.tools_progress is None
    assert "platform-tools saved" in win.log.toPlainText()


def test_gui_startup_tools_missing_download_and_failure(window, monkeypatch):
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setattr(gui, "missing_tools", lambda: ["adb"])
    _answer(monkeypatch, gui, "Download")

    def offline(progress=None):
        raise OSError("no internet")
    monkeypatch.setattr(gui, "fetch_platform_tools", offline)
    warned = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    win.check_tools()
    settle(app)
    assert warned and "no internet" in warned[0]
    assert "Download failed." in win.banner.text() and "Click here to download" in win.banner.text()
    assert not win.busy


def test_gui_about_and_menus(window, monkeypatch):
    from opx import about
    from opx.gui import about_dialog

    app, win = window
    menus = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert menus == ["File", "Phone", "Tools", "Help"]
    assert about.DEVELOPER in win.version_label.text() and "v" in win.version_label.text()
    win.show_about()
    settle(app)
    dlg = win.about_dialog
    text = " ".join(lbl.text() for lbl in dlg.findChildren(about_dialog.QLabel))
    for needle in (about.DEVELOPER, about.REPO_URL, about.DEVELOPER_URL, "ko-fi.com", about.LICENSE_NAME):
        assert needle in text, needle
    copied = []
    monkeypatch.setattr(about_dialog.QGuiApplication, "clipboard",
                        lambda: type("C", (), {"setText": lambda self, t: copied.append(t)})())
    dlg.copy_btn.click()
    assert copied and "Version:" in copied[0] and dlg.copy_btn.text() == "Copied"
    dlg.close()
    # menu reboot actions follow the same rules as the buttons
    for target, btn in win.reboot_btns.items():
        assert win.reboot_acts[target].isEnabled() == btn.isEnabled()


def test_gui_cancel_only_for_cancellable_jobs(window):
    app, win = window
    win._set_busy(True)  # e.g. downloading tools, removing apps
    assert not win.cancel_btn.isEnabled()
    win._set_busy(True, cancellable=True)  # a backup or recovery zip
    assert win.cancel_btn.isEnabled()
    win._set_busy(False)
    assert not win.cancel_btn.isEnabled() and not win.cancellable


def test_gui_connection_light(window, monkeypatch):
    from opx import device as dev

    app, win = window
    colour = lambda: win.status_dot.styleSheet()  # noqa: E731
    assert win.connection_state()[0] == "connected"
    assert "Connected" in win.status_text.text() and "Android" in win.status_text.text()
    assert win.STATUS_COLOURS["connected"] in colour()

    win.devices = [dev.Device("X1", "adb", "unauthorized")]
    win._update_status()
    assert win.connection_state()[0] == "waiting" and "allow USB debugging" in win.status_text.text()
    assert win.STATUS_COLOURS["waiting"] in colour()

    win.devices = [dev.Device("X1", "fastboot", "bootloader")]
    assert win.connection_state() == ("connected", "Connected: Bootloader (fastboot)")

    win.devices = []
    win._update_status()
    assert "Disconnected" in win.status_text.text() and win.STATUS_COLOURS["disconnected"] in colour()
    win.tools_ok = False
    assert "missing" in win.connection_state()[1]


def test_gui_poll_notices_plug_and_unplug(window, monkeypatch):
    from opx import device as dev

    app, win = window
    refreshed = []
    monkeypatch.setattr(win, "refresh_devices", lambda: refreshed.append(True))
    same = dev.presence(win.devices)
    monkeypatch.setattr(dev, "quick_scan", lambda: same)
    win.poll_devices()
    settle(app)
    assert not refreshed and not win.polling  # nothing changed: stay quiet

    monkeypatch.setattr(dev, "quick_scan", lambda: frozenset())  # unplugged
    win.poll_devices()
    settle(app)
    assert refreshed == [True] and "Phone disconnected." in win.log.toPlainText()

    win._set_busy(True)  # never during a backup / flash
    win.poll_devices()
    settle(app)
    assert refreshed == [True] and not win.polling
    win._set_busy(False)

    monkeypatch.setattr(dev, "quick_scan", lambda: None)  # adb vanished
    win.poll_devices()
    settle(app)
    assert not win.tools_ok and "missing" in win.status_text.text()


def test_gui_official_roms_pause_and_resume(window, monkeypatch, tmp_path):
    from test_catalog import PKG, SAMPLE

    from opx import catalog
    from opx.gui import catalog_dialog as cd

    app, win = window
    releases = [catalog.Release.from_json(r) for r in SAMPLE["releases"]]
    monkeypatch.setattr(cd.catalog, "for_phone", lambda model: [r for r in releases if r.model == model])
    asked = []
    monkeypatch.setattr(cd.QFileDialog, "getExistingDirectory", lambda *a, **k: asked.append(1) or str(tmp_path))
    calls = []

    def fake_download(rel, folder, progress=None, cancel=None, stage=None, android="", build=""):
        calls.append((folder, android, build))
        if len(calls) == 1:
            stage("Downloading over 4 connection(s)...")
            progress(len(PKG) // 2, len(PKG))
            (folder / (rel.filename + ".part")).write_bytes(b"half")
            raise catalog.DownloadPaused("Download paused at 1 of 2 MB. Start it again to resume.")
        stage("Resuming from 1 MB.")
        progress(len(PKG), len(PKG))
        out = folder / rel.filename
        out.write_bytes(PKG)
        return out

    monkeypatch.setattr(cd.catalog, "download", fake_download)
    dlg = cd.CatalogDialog(win, win._dialog_job, "PJZ110", "16", "BP2A.250605.015")
    settle(app)
    dlg.download()
    settle(app)
    assert dlg.download_btn.text() == "Resume download" and "paused" in dlg.status.text()
    assert dlg.progress.isVisibleTo(dlg)  # keeps showing how far it got
    dlg.download()  # resume: same folder, no second folder question
    settle(app)
    assert asked == [1] and calls[1][0] == tmp_path and dlg.downloaded == tmp_path / releases[1].filename
    assert calls[0][1:] == ("16", "BP2A.250605.015")  # the phone's own Android version and build
    log = win.log.toPlainText()
    assert "Downloading over 4 connection(s)" in log and "Download paused" in log and "Resuming from" in log
