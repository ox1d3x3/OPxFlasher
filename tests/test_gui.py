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
    from conftest import FAKE_FASTBOOT
    from test_flash import make_rom
    from test_plan import put, sized

    monkeypatch.setenv("OPX_FASTBOOT", f"{sys.executable} {FAKE_FASTBOOT}")
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
