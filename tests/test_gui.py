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
    win.refresh_devices()
    settle(app)
    return app, win


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


def test_gui_flash_flow(adb_env, fb_state, monkeypatch, tmp_path):
    import sys

    import romfactory as rf
    import sys

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
    import sys

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
    from opx.gui import app as gui_app
    from test_region import simulate_ota_overwrite

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
    from opx import backup as bk
    from opx import plan as pn
    from opx.gui.flash_dialog import FlashDialog
    from opx.session import open_session
    from test_region import rom_for

    _gui_phone(adb_env, monkeypatch)
    app = QApplication.instance() or QApplication([])
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
    from opx.gui import app as gui_app
    from test_root import STOCK, magisk_patch, rom_with_init_boot
    from test_root_cli import make_rootable

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
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    fetched = []
    monkeypatch.setattr(gui, "fetch_platform_tools", lambda: fetched.append(1) or tmp_path / "pt")

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.refresh_devices()
    settle(app)
    assert win.tools_btn.isVisibleTo(win)
    assert "missing" in win.banner.text()

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

    def fake_download(rel, folder, progress=None, cancel=None):
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
    from pathlib import Path

    import romfactory as rf

    import sys

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
