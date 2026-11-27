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



def test_gui_rom_check_without_root(adb_env, monkeypatch, tmp_path):
    """Issue #45: an unrooted phone is compared with what Android reports, never offered a flash."""
    import json

    from test_plan import PROJECT_META, rom_zip

    monkeypatch.setenv("FAKE_ACCESS", "none")
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    props = json.loads((adb_env / "props.json").read_text())
    props.update({"ro.boot.prjname": "24821", "ro.build.oplus_nv_id": "00011011"})
    (adb_env / "props.json").write_text(json.dumps(props))
    from opx.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.poll_timer.setInterval(10**9)
    win.refresh_devices()
    settle(app)
    try:
        win.tabs.setCurrentIndex(1)
        win.rom_edit.setText(str(rom_zip(tmp_path, meta=PROJECT_META).path))
        win.check_rom()
        settle(app)
        assert win.check_device.source == "props"
        table = win.checks_table
        checks = {table.item(r, 1).text(): table.item(r, 0).text() for r in range(table.rowCount())}
        assert checks["Device match"] == "OK" and checks["Root"] == "Info" and checks["Project"] == "Warning"
        assert win.flash_btn.isEnabled()  # without root: flash offered (backup recommended, not forced)
    finally:
        QThreadPool.globalInstance().waitForDone(10000)
        win.close()
        win.deleteLater()
        settle(app, 5)

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
            self.accept_arb()
            seen["enabled"] = self.start_btn.isEnabled()
            seen["preview"] = self.preview.toPlainText()
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui_app, "FlashDialog", AutoDialog)
    win.start_flash()
    settle(app, cycles=600)

    assert seen["enabled"] and "All safety gates passed" in seen["preview"]
    assert win.flash_label.text() == "Done: flash complete"
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
    assert win.sig_label.text() == "Done: restore complete"
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
    d.accept_arb()
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


def test_flash_dialog_asks_for_the_hardware_with_android_17(adb_env, fb_state, monkeypatch, tmp_path):
    import romfactory as rf

    from opx import plan as pn
    from opx.gui.flash_dialog import FlashDialog
    from opx.rom.ota import open_rom
    from opx.session import open_session

    _gui_phone(adb_env, monkeypatch)
    _app = QApplication.instance() or QApplication([])
    s = open_session("FAKE0001")
    parts = {"boot": b"B" * 8192, "abl": rf.build_qcom_elf(arb=1), "recovery": b"C" * 8192, "system": b"S" * 8192}
    payload = rf.build_payload(parts, groups={"main": ["system"]})
    rom = open_rom(rf.build_ota_zip(tmp_path / "r17.zip", payload,
                                    rf.ota_metadata(product="CPH2653", android="17")))
    device = pn.device_from_session(s)
    d = FlashDialog(None, rom, device, pn.build_report(rom, device))
    assert d.hw_cn is not None and d.hw_global is not None
    assert d.hw_cn.isChecked()  # the fake phone is a PJZ110: China hardware preselected
    assert d.options().hardware == "cn"
    assert not any("which hardware" in p for p in d.plan.problems)
    assert "abl" not in [st.partition for st in d.plan.steps]
    d.hw_global.setChecked(True)
    assert d.options().hardware == "global" and "abl" in [st.partition for st in d.plan.steps]


def test_flash_dialog_offers_the_recovery_route(adb_env, fb_state, monkeypatch, tmp_path):
    import romfactory as rf

    from opx import plan as pn
    from opx.gui import flash_dialog as fdm
    from opx.rom.ota import open_rom
    from opx.session import open_session

    _gui_phone(adb_env, monkeypatch)
    _app = QApplication.instance() or QApplication([])
    s = open_session("FAKE0001")
    parts = {"boot": b"B" * 8192, "recovery": b"C" * 8192, "system": b"S" * 8192}
    rom = open_rom(rf.build_ota_zip(tmp_path / "r.zip", rf.build_payload(parts, groups={"main": ["system"]}),
                                    rf.ota_metadata(product="PJZ110", android="16")))
    device = pn.device_from_session(s)
    d = fdm.FlashDialog(None, rom, device, pn.build_report(rom, device))
    assert d.route_pc.isChecked() and d.route_rec.isEnabled() and d.route == fdm.ROUTE_PC
    d.wipe_box.setChecked(True)
    d.route_rec.setChecked(True)
    assert d.route == fdm.ROUTE_RECOVERY
    assert not d.wipe_box.isChecked() and not d.wipe_box.isVisibleTo(d)  # the recovery zip never wipes
    text = d.preview.toPlainText()
    assert "Copy it to the phone's internal storage" in text and "Restart into AERA" in text
    d.route_pc.setChecked(True)
    assert d.route == fdm.ROUTE_PC and d.wipe_box.isVisibleTo(d)
    # the phone only in fastboot: the zip is copied while Android runs
    device.source = "fastboot"
    assert "Start the phone in Android" in fdm.recovery_route_blocker(device, pn.build_report(rom, device))


def test_phone_hardware_is_set_once_and_used_by_the_flash_window(window, tmp_path):
    import romfactory as rf

    from opx import hardware as hwm
    from opx import plan as pn
    from opx.gui.flash_dialog import FlashDialog
    from opx.rom.ota import open_rom
    from opx.session import open_session

    app, win = window
    serial = win.selected_device().serial
    assert win.hw_combo.currentData() == "" and win.hw_combo.isEnabled()
    win.hw_combo.setCurrentIndex(win.hw_combo.findData("cn"))
    win._hardware_chosen()
    assert hwm.get(serial) == "cn" and win.hw_acts["cn"].isChecked()

    s = open_session(serial)
    parts = {"boot": b"B" * 8192, "abl": rf.build_qcom_elf(arb=1), "system": b"S" * 8192}
    rom = open_rom(rf.build_ota_zip(tmp_path / "r17.zip", rf.build_payload(parts, groups={"main": ["system"]}),
                                    rf.ota_metadata(product="CPH2653", android="17")))
    device = pn.device_from_session(s)
    device.facts["model"] = "CPH2653"  # converted: the model no longer says China
    d = FlashDialog(None, rom, device, pn.build_report(rom, device))
    assert d.hw_cn.isChecked() and not d.hw_cn.isEnabled() and not d.hw_global.isEnabled()
    assert d.options().hardware == "cn"

    win._hardware_chosen(code="glo")  # changed from the Phone menu
    assert hwm.get(serial) == "glo" and win.hw_combo.currentData() == "glo"


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
    assert tab.label.text() == "Done: root complete"
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



def test_gui_flash_image_without_a_backup(window, monkeypatch, tmp_path, fb_state):
    """A phone without root (no backup possible): tick "I agree with the risk", type FLASH."""
    import os
    import sys
    from pathlib import Path

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    boot = rf.build_boot_image(b"aera-like", header_version=4)
    (tmp_path / "r.img").write_bytes(boot)
    tab = win.image_tab
    tab.part_combo.setCurrentText("boot")
    tab.image_edit.setText(str(tmp_path / "r.img"))
    tab.check()
    assert not tab.plan.ok and "backup" in tab.summary.text().lower()
    tab.no_backup.setChecked(True)
    assert not tab.backup_edit.isEnabled() and tab.confirm_label.text() == "Type FLASH:"
    tab.check()
    assert tab.plan.ok, tab.summary.text()
    assert "No backup" in tab.summary.text()
    tab.confirm.setText("FLASH")
    assert tab.flash_btn.isEnabled()
    tab.flash_btn.click()
    settle(app, 400)
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    assert (phone / "by-name" / "boot_a").read_bytes().startswith(boot), win.log.toPlainText()[-800:]

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
    # after a reboot to fastboot / recovery the export still describes the last connected phone
    win.session = None
    win.export_log()
    with zipfile.ZipFile(out) as z:
        about = z.read("about.json").decode()
    assert '"model": "PJZ110"' in about and "FAKE0001" not in about

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

    # built-in lists: one menu entry per shipped list; using one removes what's on the phone
    titles = [a.text() for a in tab.builtin_btn.menu().actions()]
    assert "ColorOS 16 CN: Recommended" in titles and tab.builtin_btn.isEnabled()
    rec = next(a for a in tab.builtin_btn.menu().actions() if a.text() == "ColorOS 16 CN: Recommended")
    assert "Minimal plus" in rec.toolTip()
    tab.restore_all()
    settle(app)
    rec.trigger()
    settle(app)
    state = json.loads((phone / "packages.json").read_text())
    assert state["com.heytap.browser"]["removed"] and not state["com.android.systemui"]["removed"]


def test_gui_ai_debloat(window, monkeypatch, tmp_path):
    import json
    import os
    from pathlib import Path

    from opx import apps as ap
    from opx import mcp_server
    from opx.gui import apps_tab, recommend_dialog

    app, win = window
    monkeypatch.setattr(ap, "undo_dir", lambda: tmp_path / "debloat")
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    tab = win.apps_tab
    tab.load()
    settle(app)

    # export: the phone's apps + a prompt, no serial
    out = tmp_path / "apps.json"
    monkeypatch.setattr(apps_tab.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    monkeypatch.setattr(apps_tab.QMessageBox, "information", lambda *a, **k: None)
    tab.export_for_ai()
    settle(app)
    inv = json.loads(out.read_text())
    assert inv["format"] == "opxflasher-apps" and "FAKE0001" not in out.read_text()
    assert out.with_suffix(".prompt.txt").exists()

    # import an AI's answer: safe + recommended ticked, aggressive not, protected never
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps({"packages": [
        {"package": "com.heytap.browser", "level": "safe", "reason": "second browser"},
        {"package": "com.oplus.games", "level": "aggressive", "reason": "game mode"},
        {"package": "com.android.systemui", "level": "safe", "reason": "wrong"}]}))
    seen = {}

    def run_dialog(dlg):
        seen["ticked"] = dlg.ticked()
        seen["go"] = dlg.go.isEnabled()
        dlg.level_boxes["aggressive"].setChecked(True)
        seen["with_aggressive"] = dlg.ticked()
        return recommend_dialog.RecommendDialog.Accepted

    monkeypatch.setattr(recommend_dialog.RecommendDialog, "exec", run_dialog)
    tab.import_recommendations(reply)
    settle(app)
    assert seen["ticked"] == ["com.heytap.browser"] and seen["go"]
    assert set(seen["with_aggressive"]) == {"com.heytap.browser", "com.oplus.games"}
    state = json.loads((phone / "packages.json").read_text())
    assert state["com.heytap.browser"]["removed"] and state["com.oplus.games"]["removed"]
    assert not state["com.android.systemui"]["removed"]
    assert tab.undo_run_btn.isEnabled()
    tab.restore_last_run()
    settle(app)
    state = json.loads((phone / "packages.json").read_text())
    assert not state["com.heytap.browser"]["removed"] and not state["com.oplus.games"]["removed"]

    # Claude Desktop: copy the config
    monkeypatch.setattr(mcp_server, "claude_config_path", lambda: tmp_path / "Claude" / "c.json")

    class Box(apps_tab.QMessageBox):
        def exec(self):
            self._copy = [b for b in self.buttons() if b.text() == "Copy config"][0]
            return 0

        def clickedButton(self):
            return self._copy

    monkeypatch.setattr(apps_tab, "QMessageBox", Box)
    tab.claude_desktop()
    assert '"opxflasher"' in app.clipboard().text()


def test_gui_send_to_op_debloater(window, monkeypatch, tmp_path):
    import json
    import os
    from pathlib import Path

    from opx import apps as ap
    from opx.gui import apps_tab, recommend_dialog

    app, win = window
    monkeypatch.setattr(ap, "undo_dir", lambda: tmp_path / "debloat")
    monkeypatch.setenv("FAKE_OP_DEBLOAT", "1")
    phone = Path(os.environ["FAKE_DEVICE_DIR"])
    tab = win.apps_tab
    tab.load()
    settle(app)
    reply = tmp_path / "picks.json"
    reply.write_text(json.dumps({"packages": [{"package": "com.heytap.browser", "level": "safe"}]}))
    monkeypatch.setattr(recommend_dialog.RecommendDialog, "exec", lambda self: recommend_dialog.OP_DEBLOATER)
    monkeypatch.setattr(apps_tab.QMessageBox, "question", lambda *a, **k: apps_tab.QMessageBox.Yes)
    monkeypatch.setattr(apps_tab.QMessageBox, "information", lambda *a, **k: None)
    tab.import_recommendations(reply)
    settle(app)
    sent = list((phone / "sdcard" / "debloater" / "import").glob("op-debloat_*.json"))
    assert len(sent) == 1 and json.loads(sent[0].read_text())["packages"] == ["com.heytap.browser"]
    assert json.loads((phone / "packages.json").read_text())["com.heytap.browser"]["removed"]

    # Save my list... in the module's format
    out = tmp_path / "mine.json"
    monkeypatch.setattr(apps_tab.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "OP Debloater (*.json)"))
    ap.apply(win.session.adb, win.session.serial, ["com.oplus.games"], [])
    tab.export_list()
    assert json.loads(out.read_text())["format"] == "op-debloat" and out.with_suffix(".list").exists()


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
    assert menus == ["File", "Phone", "ROMs", "Tools", "Help"]
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


def test_gui_convert_to_full_rom(window, tmp_path, monkeypatch):
    from test_convert import make_ota

    from opx.gui import convert_dialog as cdlg

    app, win = window
    ota, _ = make_ota(tmp_path)
    win.rom_edit.setText(str(ota))
    out = tmp_path / "converted"
    out.mkdir()
    dlg = cdlg.ConvertDialog(win, win._dialog_job, str(ota))
    settle(app)
    text = dlg.report.toPlainText()
    assert "Full OTA zip" in text and "PJZ110" in text and dlg.convert_btn.isEnabled()
    dlg.out_edit.setText(str(out))
    assert dlg.want_folder.isChecked() and not dlg.want_zip.isChecked()  # OPxFlasher first; recovery zip opt-in
    dlg.want_zip.setChecked(True)
    dlg.start()
    for _ in range(200):
        settle(app, 5)
        if dlg.made:
            break
    assert set(dlg.made) == {"zip", "folder"} and dlg.made["zip"].exists()
    assert "Recovery zip:" in dlg.status.text() and dlg.use_btn.isVisibleTo(dlg)
    assert not dlg.convert_btn.isEnabled()  # done; no second run into the same names
    assert dlg.result_path == dlg.made["folder"]
    log = win.log.toPlainText()
    assert "Convert: Full OTA zip" in log and "Recovery zip:" in log

    # ROM check shows what the ROM is
    win.rom_edit.setText(str(dlg.made["zip"]))
    win.check_rom()
    for _ in range(200):
        settle(app, 5)
        if "Detected:" in win.log.toPlainText():
            break
    assert "Detected: OPxFlasher full ROM (zip) for PJZ110" in win.log.toPlainText()
    assert "  Note: OPxFlasher full ROM, converted from" in win.log.toPlainText()
    assert "OPxFlasher full ROM (zip)" in win.rom_summary.text()


def test_gui_convert_android_17_zip_asks_the_hardware(window, tmp_path):
    from test_rom_install import _ota17

    from opx.gui import convert_dialog as cdlg

    app, win = window
    ota, _ = _ota17(tmp_path)
    dlg = cdlg.ConvertDialog(win, win._dialog_job, str(ota))
    settle(app)
    dlg.out_edit.setText(str(tmp_path / "o"))
    assert not dlg.hw_cn.isVisibleTo(dlg)  # image folder only: asked when flashing
    dlg.want_zip.setChecked(True)
    assert dlg.hw_cn.isVisibleTo(dlg) and dlg.hw_global.isVisibleTo(dlg)
    dlg.start()
    assert "China or Global" in dlg.status.text() and not dlg.busy
    dlg.want_folder.setChecked(False)
    dlg.hw_cn.setChecked(True)
    dlg.start()
    for _ in range(200):
        settle(app, 5)
        if dlg.made:
            break
    assert dlg.made["zip"].name.endswith("-cnhw.zip")


def test_gui_convert_refuses_incremental(window, tmp_path):
    from test_convert import make_ota

    from opx.gui import convert_dialog as cdlg

    app, win = window
    ota, _ = make_ota(tmp_path, incremental=True)
    dlg = cdlg.ConvertDialog(win, win._dialog_job, str(ota))
    settle(app)
    assert not dlg.convert_btn.isEnabled() and "incremental" in dlg.status.text()


def test_gui_catalog_snapshot_menu(window, monkeypatch, tmp_path):
    from opx import catalog, settings
    from opx.gui import app as gui

    app, win = window
    monkeypatch.setattr(settings, "path", lambda: tmp_path / "settings.json")
    snap = tmp_path / "snap.json"
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(snap), ""))
    monkeypatch.setattr(catalog, "snapshot", lambda out: {"count": 3, "devices": 1, "path": out})
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    win.save_catalog_snapshot()
    settle(app)
    assert shown and "3 releases" in shown[0] and settings.get("catalog_snapshot") == str(snap)
    assert "Catalog snapshot: 3 releases" in win.log.toPlainText()

    other = tmp_path / "other.json"
    other.write_text('{"releases": []}')
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileName", lambda *a, **k: (str(other), ""))
    win.use_catalog_snapshot()
    assert settings.get("catalog_snapshot") == str(other)
    roms = win.menuBar().actions()[2].menu()
    sub = next(a.menu() for a in roms.actions() if a.text() == "ROM catalog")
    menu = [a.text() for a in sub.actions()]
    assert "Save ROM catalog snapshot..." in menu and "Use a ROM catalog snapshot..." in menu


def test_gui_rom_check_without_a_phone_is_calm(window, tmp_path):
    from test_plan import rom_zip

    app, win = window
    win.cmp_live.setChecked(True)
    win.session = None
    win.devices = []
    win.device_combo.clear()
    rom = rom_zip(tmp_path)
    win.rom_edit.setText(str(rom.path))
    win.check_rom()
    for _ in range(100):
        settle(app, 5)
        if win.report is not None:
            break
    assert win.report.verdict == "NOT COMPARED", [(c.name, c.status) for c in win.report.checks]
    assert "connect your phone" in win.verdict.text() and not win.reasons.isVisibleTo(win)
    assert win.checks_table.item(0, 1).text() == "Phone"  # the one thing to do comes first
    assert win.checks_table.item(0, 0).text() == "Next"  # shown as a step, not a red STOP
    assert "BLOCK " not in win.log.toPlainText().split("ROM check:")[-1]


def test_gui_verify_rom_then_offer_conversion(window, tmp_path, monkeypatch):
    from test_convert import LOGICAL, make_ota
    from test_verify import boot_set

    from opx.gui import verify_dialog as vd

    app, win = window
    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    asked = []
    monkeypatch.setattr(vd.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    dlg = vd.VerifyDialog(win, win._dialog_job, str(ota))
    dlg.start()
    for _ in range(200):
        settle(app, 5)
        if dlg.report is not None:
            break
    assert dlg.report.verdict.startswith("VERIFIED") and dlg.verdict.text().startswith("VERIFIED")
    assert dlg.table.rowCount() > 3 and "OrangeFox / AERA: After converting" in dlg.targets.text()
    assert asked and "Convert now?" in asked[0] and "boot" in asked[0]  # found files listed
    assert dlg.convert_btn.isEnabled() and not dlg.convert_requested
    assert "Verify: VERIFIED" in win.log.toPlainText()


def test_gui_unlock_dialog_gates(window, monkeypatch):
    import random

    from opx.gui import unlock_dialog as ud

    app, win = window
    dlg = ud.UnlockDialog(win, seconds=3, rng=random.Random(1))
    assert not dlg.go.isEnabled() and "3 s" in dlg.timer_label.text()
    dlg.agree.setChecked(True)
    dlg.answer.setText(str(dlg.a + dlg.b))
    assert not dlg.go.isEnabled()  # still reading
    for _ in range(3):
        dlg._tick()
    assert dlg.go.isEnabled()
    dlg.answer.setText(str(dlg.a + dlg.b + 1))
    assert not dlg.go.isEnabled()  # wrong sum
    dlg.answer.setText(str(dlg.a + dlg.b))
    dlg.agree.setChecked(False)
    assert not dlg.go.isEnabled()  # no agreement
    dlg.agree.setChecked(True)
    monkeypatch.setattr(ud.QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg._confirm()
    assert dlg.result() != QDialog.DialogCode.Accepted
    monkeypatch.setattr(ud.QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._confirm()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert all(w.split()[0] in " ".join(lb.text() for lb in dlg.findChildren(ud.QLabel)) for w in ud.ul.WARNINGS)


def test_gui_download_and_convert(window, monkeypatch, tmp_path):
    from test_catalog import SAMPLE
    from test_convert import LOGICAL, make_ota
    from test_verify import boot_set

    from opx import catalog
    from opx.gui import catalog_dialog as cd

    app, win = window
    releases = [catalog.Release.from_json(r) for r in SAMPLE["releases"]]
    monkeypatch.setattr(cd.catalog, "for_phone", lambda model: [r for r in releases if r.model == model])
    monkeypatch.setattr(cd.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "dl"))
    (tmp_path / "dl").mkdir()
    src, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})

    def fake_download(rel, folder, progress=None, cancel=None, stage=None, android="", build=""):
        out = folder / src.name
        out.write_bytes(src.read_bytes())
        return out

    monkeypatch.setattr(cd.catalog, "download", fake_download)
    dlg = cd.CatalogDialog(win, win._dialog_job, "PJZ110", "16", "BP2A.250605.015")
    settle(app)
    assert dlg.for_opx.isChecked() and not dlg.for_recovery.isChecked()  # OPxFlasher by default
    dlg.for_opx.setChecked(False)
    assert not dlg.convert_btn.isEnabled()  # nothing to make
    dlg.for_opx.setChecked(True)
    dlg.download_and_convert()
    for _ in range(200):
        settle(app, 5)
        if dlg.converted:
            break
    assert set(dlg.converted) == {"folder"} and dlg.converted["folder"].is_dir()
    assert dlg.result_path == dlg.converted["folder"] and dlg.downloaded.exists()
    assert "Convert: Image folder:" in win.log.toPlainText()


def test_gui_closing_a_busy_dialog_stops_the_job_instead(window, tmp_path):
    from opx.gui import verify_dialog as vd

    app, win = window
    f = tmp_path / "x.zip"
    f.write_bytes(b"not a rom")
    dlg = vd.VerifyDialog(win, lambda *a, **k: None, str(f))  # a job that never finishes
    dlg.show()
    dlg.start()
    assert dlg.busy
    dlg.reject()  # Esc / window X
    assert dlg.isVisible() and dlg.cancel.is_set() and "Stopping" in dlg.status.text()
    dlg._failed(vd.vf.VerifyCancelled())
    dlg.reject()
    assert not dlg.isVisible()


def test_gui_rom_box_shows_what_the_rom_is_as_soon_as_it_is_chosen(window, tmp_path):
    from test_convert import LOGICAL, make_ota
    from test_verify import boot_set

    app, win = window
    ota, _ = make_ota(tmp_path, parts={**boot_set(), **{n: os.urandom(8192) for n in LOGICAL}})
    named = ota.with_name("PJZ110_11.C.93_1930_202608031342.zip")
    ota.rename(named)
    win.rom_info_timer.setInterval(0)  # no typing pause in the test
    win.rom_edit.setText(str(named))
    for _ in range(100):
        settle(app, 5)
        if "OS:" in win.rom_info.text():
            break
    text = win.rom_info.text()
    assert not win.rom_info.isHidden() and "OS: Android" in text and "OTA 11.C.93" in text
    assert "For: PJZ110 (CN" in text and "Full OTA" in text and "images" in text
    bad = tmp_path / "notes.txt"
    bad.write_text("hi")
    win.rom_edit.setText(str(bad))
    for _ in range(100):
        settle(app, 5)
        if "OS:" not in win.rom_info.text() and "Reading" not in win.rom_info.text():
            break
    assert "#ff6b63" in win.rom_info.text()  # not a ROM: said plainly, in the box
    win.rom_edit.setText("")
    settle(app, 80)
    assert win.rom_info.isHidden()


def test_gui_style_sheet_finds_the_tick_image():
    from pathlib import Path

    from opx.gui import app as gui

    css = gui.style_sheet()
    assert "CHECK_PNG" not in css
    path = css.split("image: url(", 1)[1].split(")", 1)[0]
    assert Path(path).is_file()


def test_gui_panel_in_fastboot_shows_what_it_can(window, monkeypatch, fb_state):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    assert win.last_facts.get("serial") == "FAKE0001"
    fb_state(mode="bootloader")
    win.refresh_devices()
    settle(app, 300)
    labels = win.info_labels
    assert labels["bootloader"].text() == "Unlocked"
    assert labels["slot_suffix"].text() == "_a"
    assert labels["model"].text() == "PJZ110" and "last seen in Android" in labels["build"].text()
    assert labels["access"].text() == "bootloader"


def test_gui_phone_animation_follows_the_phone(window, monkeypatch, fb_state):
    import sys

    from conftest import FAKE_FASTBOOT, tool_cmd

    from opx.gui import phone_anim

    app, win = window
    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    assert win.phone_anim.state == "android"
    fb_state(mode="bootloader")
    win.refresh_devices()
    settle(app, 300)
    assert win.phone_anim.state == "bootloader"
    win.phone_anim.grab()  # paints without errors
    assert phone_anim.state_for(None) == "none"

    class D:
        transport, state = "fastboot", "fastbootd"
    assert phone_anim.state_for(D) == "fastbootd"
    D.transport, D.state = "adb", "unauthorized"
    assert phone_anim.state_for(D) == "unauthorized"


def test_gui_is_organised_by_task(window):
    app, win = window
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert names == ["Backup", "ROMs", "Flash ROM", "Root", "Flash image", "Signal / region", "Apps", "Diagnostics"]
    assert all(win.tabs.tabToolTip(i) for i in range(win.tabs.count()))
    menus = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert menus == ["File", "Phone", "ROMs", "Tools", "Help"]
    used = [a.text() for a in win.use_backup_btn.menu().actions()]
    assert used == ["Verify a backup...", "Restore full ROM...", "Make recovery zip..."]


def test_gui_flash_without_root_or_backup(adb_env, fb_state, monkeypatch, tmp_path):
    # a phone without root can't be backed up: the flash is offered, NO BACKUP is typed
    import sys

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_flash import make_rom
    from test_plan import put, sized

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    monkeypatch.setenv("FAKE_ACCESS", "none")
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
    try:
        win.out_edit.setText(str(tmp_path / "out"))
        rom = make_rom(tmp_path)
        win.rom_edit.setText(str(rom.path))
        win.check_rom()
        settle(app)
        assert win.check_device.source == "props" and win.flash_btn.isEnabled()
        seen = {}

        class AutoDialog(gui_app.FlashDialog):
            def exec(self):
                self.accept_arb()
                self.confirm_edit.setText("FLASH")
                seen["blocked_without"] = not self.start_btn.isEnabled()  # no backup chosen, box not ticked
                self.no_backup.setChecked(True)  # "I agree with the risk": no extra word to type
                seen["word"] = self.no_backup.text()
                seen["enabled"] = self.start_btn.isEnabled()
                seen["preview"] = self.preview.toPlainText()
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(gui_app, "FlashDialog", AutoDialog)
        win.start_flash()
        settle(app, cycles=600)
        assert seen["blocked_without"] and "I agree with the risk" in seen["word"] and seen["enabled"]
        assert "No backup" in seen["preview"]
        assert win.flash_label.text() == "Done: flash complete"
        assert (adb_env / "by-name" / "dtbo_b").read_bytes().startswith(b"D" * 4096)
    finally:
        QThreadPool.globalInstance().waitForDone(10000)
        win.close()
        win.deleteLater()
        settle(app, 5)


def test_rom_check_in_fastboot_uses_what_android_reported(adb_env, tmp_path):
    from test_flash import make_rom

    from opx.gui.app import run_rom_check
    from opx.session import open_session

    rom = make_rom(tmp_path)
    props = open_session("FAKE0001").props
    _, device, report = run_rom_check(str(rom.path), "", None, "FAKE0001", props, "fastbootd")
    assert device.source == "fastboot" and report.compared
    # never seen in Android (e.g. Android doesn't start): known from the bootloader only
    import opx.gui.app as gui_app

    monkeypatch_info = {"product": "nothing-known", "current-slot": "a"}
    orig = gui_app.support.fastboot_info
    gui_app.support.fastboot_info = lambda serial: monkeypatch_info
    try:
        _, device, report = run_rom_check(str(rom.path), "", None, "FAKE0001", None, "fastbootd")
    finally:
        gui_app.support.fastboot_info = orig
    assert device.source == "fastboot" and "bootloader only" in device.label
    assert report.verdict == "BLOCKED"  # an unknown phone: only with the owner's confirmation


def test_gui_arb_risk_dialog_gates(window, monkeypatch):
    # anti-rollback the app can't check: a minute of reading, a sum, the agreement, then "Are you sure?"
    import random

    from opx.gui import arb_dialog as ad

    app, win = window
    assert ad.READ_SECONDS == 60
    dlg = ad.ArbRiskDialog(win, ["Qualcomm ARB abl: The phone's value can't be read without root (ROM ?, phone ?)."],
                           seconds=2, rng=random.Random(2))
    text = " ".join(lb.text() for lb in dlg.findChildren(ad.QLabel))
    assert "service centre" in text and "permanent" in text and "ROM ?, phone ?" in text
    assert not dlg.go.isEnabled()
    dlg.agree.setChecked(True)
    dlg.answer.setText(str(dlg.a + dlg.b))
    assert not dlg.go.isEnabled()  # still reading
    dlg._tick()
    dlg._tick()
    assert dlg.go.isEnabled()
    dlg.answer.setText("0")
    assert not dlg.go.isEnabled()
    dlg.answer.setText(str(dlg.a + dlg.b))
    monkeypatch.setattr(ad.QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg._confirm()
    assert dlg.result() != QDialog.DialogCode.Accepted
    monkeypatch.setattr(ad.QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._confirm()
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_gui_arb_risk_dialog_refuses_an_older_rom(window, monkeypatch):
    # Validate found the ROM older than the installed build: no way to accept
    import random

    from opx import arb
    from opx.gui import arb_dialog as ad

    app, win = window
    older = arb.Validation("SER1", "rom", "16.0.7.201", "16.0.10.501", arb.OLDER, "The ROM is OLDER. DON'T flash it.")
    dlg = ad.ArbRiskDialog(win, [], seconds=0, rng=random.Random(1), validation=older)
    dlg.agree.setChecked(True)
    dlg.answer.setText(str(dlg.a + dlg.b))
    assert dlg.refused and not dlg.go.isEnabled() and "OLDER" in dlg.validation_label.text()
    # Validate runs again (phone updated / other ROM): the answer replaces the old one
    same = arb.Validation("SER1", "rom", "16.0.10.501", "16.0.10.501", arb.SAME, "The ROM is the SAME build.")
    dlg2 = ad.ArbRiskDialog(win, [], seconds=0, rng=random.Random(1), validation=older, validate=lambda: same)
    dlg2.agree.setChecked(True)
    dlg2.answer.setText(str(dlg2.a + dlg2.b))
    assert not dlg2.go.isEnabled()
    dlg2.validate_btn.click()
    assert dlg2.go.isEnabled() and "SAME" in dlg2.validation_label.text()


def test_gui_roms_and_flash_tabs_share_the_rom(window):
    # ROMs: get / verify / convert; Flash ROM: choose, check, flash. One ROM for both.
    app, win = window
    win.build_rom_edit.setText("/x/rom.zip")
    assert win.rom_edit.text() == "/x/rom.zip"
    win.rom_edit.setText("/y/folder")
    assert win.build_rom_edit.text() == "/y/folder"
    assert win.cmp_backup_edit.isHidden() and not win.cmp_more.isChecked()  # backup compare tucked away
    win.cmp_more.setChecked(True)
    assert not win.cmp_backup_edit.isHidden()
    win.cmp_more.setChecked(False)
    assert win.cmp_live.isChecked()
    assert "Phone:" in win.flash_phone.text()


def test_gui_arb_risk_dialog_compares_a_typed_build(window):
    # phone in fastboot only: the owner types the installed build; an older ROM is refused
    import random

    from opx import arb
    from opx.gui import arb_dialog as ad

    app, win = window

    def compare(text):
        result = arb.OLDER if "16.0.11" in text else arb.SAME
        return arb.Validation("S", "rom", "16.0.10.501", text, result, f"{result.upper()} build")

    dlg = ad.ArbRiskDialog(win, [], seconds=0, rng=random.Random(3), compare=compare)
    dlg.agree.setChecked(True)
    dlg.answer.setText(str(dlg.a + dlg.b))
    assert dlg.go.isEnabled()  # not typed: the owner can still accept (with the warning read)
    dlg.build_edit.setText("CPH2653_16.0.11.100(EX01)")
    dlg._run_compare()
    assert dlg.refused and not dlg.go.isEnabled()
    dlg.build_edit.setText("CPH2653_16.0.10.501(EX01)")
    dlg._run_compare()
    assert dlg.go.isEnabled() and "SAME" in dlg.validation_label.text()
    text = " ".join(lb.text() for lb in dlg.findChildren(ad.QLabel))
    assert "newest official ROM" in text


def test_gui_resume_a_stopped_flash(adb_env, fb_state, monkeypatch, tmp_path):
    # the GUI resumes a stopped flash from its journal, as opx flash --resume does
    import sys

    import romfactory as rf
    from conftest import FAKE_FASTBOOT, tool_cmd
    from test_flash import make_rom
    from test_plan import put, sized

    from opx import flash as fl
    from opx import plan as pn
    from opx.session import open_session
    from opx.tools import Tool

    monkeypatch.setenv("OPX_FASTBOOT", tool_cmd(sys.executable, FAKE_FASTBOOT))
    for slot in ("_a", "_b"):
        put(adb_env, f"vbmeta{slot}", sized(rf.build_vbmeta(2)))
        put(adb_env, f"xbl_config{slot}", sized(rf.build_qcom_elf(arb=1)))
        put(adb_env, f"dtbo{slot}", b"\0" * 65536)
    rom = make_rom(tmp_path)
    s = open_session("FAKE0001")
    device = pn.device_from_session(s)
    opts = fl.FlashOptions(overrides=list(fl.RISK_OVERRIDES), allow_fastbootd=True, no_backup=True)
    plan = fl.prepare(rom, pn.build_report(rom, device), device, opts, None)
    assert plan.ok, plan.problems
    fb_state(fail_flash="system")
    journal = tmp_path / "j" / "journal.json"
    journal.parent.mkdir()
    runner = fl.Runner(plan, rom, s.profile, Tool("adb"), Tool("fastboot"), journal, tmp_path / "w", wait_timeout=5)
    try:
        runner.run()
    except fl.FlashError:
        pass
    fb_state(fail_flash=None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)  # the "Flash stopped" box, if it fails
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    from opx.gui import app as gui_app

    app = QApplication.instance() or QApplication([])
    win = gui_app.MainWindow()
    try:
        win.resume_flash(journal)
        settle(app, cycles=600)
        steps = fl.load_journal(journal).steps
        assert win.flash_label.text() == "Done: flash complete", [(st.label, st.status, st.detail) for st in steps
                                                                  if st.status != "done"]
        assert all(st.status == "done" for st in steps)
    finally:
        QThreadPool.globalInstance().waitForDone(10000)
        win.close()
        win.deleteLater()
        settle(app, 5)


def test_gui_custom_recovery_from_the_owners_download(window, monkeypatch, tmp_path):
    import hashlib
    import zipfile

    import romfactory as rf

    from opx.gui import recovery_dialog as rd

    app, win = window
    rec = rf.build_boot_image(b"aera", header_version=4).ljust(2 * 1024 * 1024, b"\0")
    z = tmp_path / "AERA-R1.0.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("recovery.img", rec)
    dlg = rd.RecoveryDialog(win, [("OnePlus 13", {"name": "AERA", "url": "https://aera-recovery.com/devices/dodge"})],
                            tmp_path / "w")
    dlg.load(z)
    assert dlg.loaded and "recovery.img" in dlg.info.text()
    assert dlg.use_btn.isEnabled() and "Not compared" in dlg.verdict.text()
    dlg.sha_edit.setText("f" * 64)
    assert not dlg.use_btn.isEnabled() and "NOT match" in dlg.verdict.text()
    dlg.sha_edit.setText(hashlib.sha256(z.read_bytes()).hexdigest())
    assert dlg.use_btn.isEnabled() and "Matches" in dlg.verdict.text()
    bad = tmp_path / "x.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "nothing")
    dlg.load(bad)
    assert dlg.loaded is None and not dlg.use_btn.isEnabled()

    # from the Image tab: the dialog's image lands in recovery, both slots
    class Fake:
        def __init__(self, parent, entries, work):
            Fake.entries = entries
            self.loaded = type("L", (), {"image": tmp_path / "w" / "custom-recovery.img"})()

        def exec(self):
            return rd.QDialog.DialogCode.Accepted

    monkeypatch.setattr(rd, "RecoveryDialog", Fake)
    tab = win.image_tab
    tab.rec_btn.click()
    assert any(e["name"] == "AERA" for _, e in Fake.entries)
    assert tab.part_combo.currentText() == "recovery" and tab.slot_combo.currentData() == "both"
    assert tab.image_edit.text().endswith("custom-recovery.img")
