"""OPxFlasher desktop app (PySide6). Phase 1: read-only backup."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opx import __version__
from opx import backup as bk
from opx import device as dev
from opx.profile import MODE_TEXT, MODES
from opx import flash as fl
from opx import plan as pn
from opx.gui.flash_dialog import FlashDialog
from opx.gui.icon import app_icon, set_windows_app_id
from opx.gui.root_tab import RootTab
from opx.rom.ota import open_rom
from opx.session import Session, open_session
from opx.tools import Tool, fetch_platform_tools

STYLE = """
QMainWindow, QDialog, QMessageBox { background: #15171c; }
QWidget { color: #e6e8ee; font-size: 10pt; }
QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled { color: #6b7280; }
QScrollArea { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QGroupBox { border: 1px solid #2c3039; border-radius: 8px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9aa3b5; }
QPushButton { background: #262a33; border: 1px solid #353a45; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: #2f3440; }
QPushButton:disabled { color: #6b7280; }
QPushButton#primary { background: #d6322c; border-color: #d6322c; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #e2453f; }
QPushButton#primary:disabled { background: #4a2a29; border-color: #4a2a29; }
QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
    background: #1c1f26; border: 1px solid #2c3039; border-radius: 6px; padding: 4px;
}
QHeaderView::section { background: #20232b; border: none; padding: 4px; color: #9aa3b5; }
QProgressBar { background: #1c1f26; border: 1px solid #2c3039; border-radius: 6px; text-align: center; }
QProgressBar::chunk { background: #d6322c; border-radius: 6px; }
QLabel#banner { border-radius: 6px; padding: 8px; }
QLabel#title { font-size: 20pt; font-weight: 700; }
QLabel#verdict { font-size: 13pt; font-weight: 700; padding: 8px; border-radius: 6px; }
QTabWidget::pane { border: 1px solid #2c3039; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #1c1f26; border: 1px solid #2c3039; padding: 6px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #262a33; color: white; }
QScrollBar:vertical, QScrollBar:horizontal { background: #1c1f26; border: none; width: 10px; height: 10px; }
QScrollBar::handle { background: #3a3f4b; border-radius: 4px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
"""


def human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def make_table(headers: list[str], stretch: int) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    header = table.horizontalHeader()
    for col in range(len(headers)):
        mode = QHeaderView.Stretch if col == stretch else QHeaderView.ResizeToContents
        header.setSectionResizeMode(col, mode)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setWordWrap(True)
    return table


class JobSignals(QObject):
    done = Signal(object)
    failed = Signal(object)
    event = Signal(object)


class Job(QRunnable):
    """Runs a blocking function off the UI thread."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = JobSignals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:  # report everything to the UI
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"OPxFlasher {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1180, 860)
        self.pool = QThreadPool.globalInstance()
        self.jobs: list[Job] = []
        self.devices: list[dev.Device] = []
        self.session: Session | None = None
        self.plan: bk.Plan | None = None
        self.cancel_flag = threading.Event()
        self.last_folder: Path | None = None
        self.busy = False
        self._build_ui()
        self._update_buttons()

    # --- layout -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(10)

        title = QLabel("OPxFlasher")
        title.setObjectName("title")
        subtitle = QLabel("AIO tool for OnePlus / OPPO devices: backup, ROM check, flashing, signal restore and root.")
        subtitle.setStyleSheet("color:#9aa3b5")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # device
        dev_box = QGroupBox("Phone")
        dev_layout = QVBoxLayout(dev_box)
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.currentIndexChanged.connect(self._device_selected)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)
        dev_layout.addLayout(row)
        self.tools_btn = QPushButton("Download adb and fastboot")
        self.tools_btn.setToolTip("Downloads Google's official platform-tools (Google's terms apply)")
        self.tools_btn.clicked.connect(self.download_tools)
        self.tools_btn.hide()
        dev_layout.addWidget(self.tools_btn)

        self.info_labels: dict[str, QLabel] = {}
        form = QFormLayout()
        for key, text in (
            ("model", "Model"),
            ("build", "Build"),
            ("android", "Android"),
            ("security_patch", "Security patch"),
            ("slot_suffix", "Active slot"),
            ("access", "Root access"),
            ("profile", "Profile"),
        ):
            lbl = QLabel("-")
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info_labels[key] = lbl
            form.addRow(text + ":", lbl)
        dev_layout.addLayout(form)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        dev_layout.addWidget(self.banner)
        dev_box.setMinimumWidth(340)
        dev_box.setMaximumWidth(400)
        dev_layout.addStretch(1)

        # --- Backup tab ---------------------------------------------------
        backup_tab = QWidget()
        backup_layout = QVBoxLayout(backup_tab)
        opt_box = QGroupBox("Backup")
        opt_layout = QVBoxLayout(opt_box)
        self.mode_group = QButtonGroup(self)
        for i, mode in enumerate(MODES):
            rb = QRadioButton(MODE_TEXT[mode])
            rb.setProperty("mode", mode)
            if mode == "critical":
                rb.setChecked(True)
            self.mode_group.addButton(rb, i)
            opt_layout.addWidget(rb)
        self.mode_group.buttonToggled.connect(lambda *_: self._invalidate_plan())

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit(str(Path.home() / "OPxFlasher-backups"))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        out_row.addWidget(QLabel("Save to:"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(browse)
        opt_layout.addLayout(out_row)
        self.verify_box = QCheckBox("Verify every image on the phone with sha256sum (recommended)")
        self.verify_box.setChecked(True)
        opt_layout.addWidget(self.verify_box)

        btn_row = QHBoxLayout()
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.clicked.connect(self.preview)
        self.start_btn = QPushButton("Start backup")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_backup)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.clicked.connect(self._open_folder)
        self.verify_btn = QPushButton("Verify a backup...")
        self.verify_btn.clicked.connect(self.verify_existing)
        for b in (self.preview_btn, self.start_btn, self.cancel_btn, self.open_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(self.verify_btn)
        opt_layout.addLayout(btn_row)
        backup_layout.addWidget(opt_box)

        self.item_label = QLabel("Idle")
        self.item_bar = QProgressBar()
        self.item_bar.setFormat("Current partition  %p%")
        self.overall_bar = QProgressBar()
        self.overall_bar.setFormat("Overall  %p%")
        backup_layout.addWidget(self.item_label)
        backup_layout.addWidget(self.item_bar)
        backup_layout.addWidget(self.overall_bar)

        self.table = make_table(["Partition", "Size", "Path", "Status"], stretch=2)
        backup_layout.addWidget(self.table, 1)

        # --- ROM check tab ------------------------------------------------
        rom_tab = QWidget()
        rom_layout = QVBoxLayout(rom_tab)
        rom_box = QGroupBox("Firmware to check (nothing is flashed)")
        rom_form = QVBoxLayout(rom_box)
        rom_row = QHBoxLayout()
        self.rom_edit = QLineEdit()
        self.rom_edit.setPlaceholderText("Full OTA zip, payload.bin, or a folder of .img files")
        pick_file = QPushButton("File...")
        pick_file.clicked.connect(self._choose_rom_file)
        pick_folder = QPushButton("Folder...")
        pick_folder.clicked.connect(self._choose_rom_folder)
        rom_row.addWidget(QLabel("ROM:"))
        rom_row.addWidget(self.rom_edit, 1)
        rom_row.addWidget(pick_file)
        rom_row.addWidget(pick_folder)
        rom_form.addLayout(rom_row)

        cmp_row = QHBoxLayout()
        self.cmp_group = QButtonGroup(self)
        self.cmp_live = QRadioButton("Connected phone")
        self.cmp_live.setChecked(True)
        self.cmp_backup = QRadioButton("Backup folder:")
        self.cmp_group.addButton(self.cmp_live)
        self.cmp_group.addButton(self.cmp_backup)
        self.cmp_backup_edit = QLineEdit()
        self.cmp_backup_edit.setPlaceholderText("An OPxFlasher backup (Full mode gives the most checks)")
        pick_backup = QPushButton("Browse...")
        pick_backup.clicked.connect(self._choose_compare_backup)
        cmp_row.addWidget(QLabel("Compare with:"))
        cmp_row.addWidget(self.cmp_live)
        cmp_row.addWidget(self.cmp_backup)
        cmp_row.addWidget(self.cmp_backup_edit, 1)
        cmp_row.addWidget(pick_backup)
        rom_form.addLayout(cmp_row)

        act_row = QHBoxLayout()
        self.check_btn = QPushButton("Check ROM")
        self.check_btn.setObjectName("primary")
        self.check_btn.clicked.connect(self.check_rom)
        self.save_report_btn = QPushButton("Save report...")
        self.save_report_btn.clicked.connect(self._save_report)
        self.save_report_btn.setEnabled(False)
        act_row.addWidget(self.check_btn)
        act_row.addWidget(self.save_report_btn)
        self.flash_btn = QPushButton("Flash...")
        self.flash_btn.setEnabled(False)
        self.flash_btn.setToolTip("Writes to the phone. Needs a passing ROM check on the connected phone and a backup.")
        self.flash_btn.clicked.connect(self.start_flash)
        self.stop_flash_btn = QPushButton("Stop after this step")
        self.stop_flash_btn.setEnabled(False)
        self.stop_flash_btn.clicked.connect(self._stop_flash)
        act_row.addWidget(self.flash_btn)
        act_row.addWidget(self.stop_flash_btn)
        act_row.addStretch(1)
        rom_form.addLayout(act_row)
        rom_layout.addWidget(rom_box)

        self.verdict = QLabel("Choose a ROM and press Check ROM.")
        self.verdict.setObjectName("verdict")
        self.verdict.setWordWrap(True)
        self.rom_summary = QLabel("")
        self.rom_summary.setStyleSheet("color:#9aa3b5")
        self.rom_summary.setWordWrap(True)
        self.reasons = QLabel("")
        self.reasons.setWordWrap(True)
        self.reasons.setStyleSheet("color:#ffb3ad")
        self.reasons.hide()
        rom_layout.addWidget(self.verdict)
        rom_layout.addWidget(self.reasons)
        rom_layout.addWidget(self.rom_summary)
        rom_split = QSplitter(Qt.Vertical)
        self.checks_table = make_table(["Result", "Check", "Details"], stretch=2)
        self.plan_table = make_table(["Partition", "Size", "Action", "Target", "Note"], stretch=4)
        rom_split.addWidget(self.checks_table)
        rom_split.addWidget(self.plan_table)
        self.flash_label = QLabel("")
        self.flash_label.hide()
        self.flash_bar = QProgressBar()
        self.flash_bar.setFormat("Flashing  %p%")
        self.flash_bar.hide()
        rom_layout.addWidget(self.flash_label)
        rom_layout.addWidget(self.flash_bar)
        rom_layout.addWidget(rom_split, 1)
        self.check_rom_obj = None
        self.check_device = None
        self.flash_cancel = threading.Event()
        self.report: pn.Report | None = None

        # --- assemble -----------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.addTab(backup_tab, "Backup")
        self.tabs.addTab(rom_tab, "ROM check")
        self.tabs.addTab(self._build_signal_tab(), "Signal / region")
        self.root_tab = RootTab(self)
        self.tabs.addTab(self.root_tab, "Root")
        upper = QWidget()
        upper_layout = QHBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.addWidget(dev_box)
        upper_layout.addWidget(self.tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        body = QSplitter(Qt.Vertical)
        body.addWidget(upper)
        body.addWidget(self.log)
        body.setSizes([660, 110])
        layout.addWidget(body, 1)

        self.setCentralWidget(root)

    # --- helpers ----------------------------------------------------------

    def say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def mode(self) -> str:
        return self.mode_group.checkedButton().property("mode")

    def run_job(self, job: Job, done, failed=None, event=None) -> None:
        # Slots are bound methods of this window, so Qt queues them onto the UI thread.
        self.jobs = self.jobs[-20:] + [job]  # keep references alive while running
        job.signals.done.connect(done)
        job.signals.failed.connect(failed or self._job_failed)
        if event:
            job.signals.event.connect(event)
        self.pool.start(job)

    def _job_failed(self, exc: BaseException) -> None:
        self._set_busy(False)
        self.say(f"Error: {exc}")
        QMessageBox.warning(self, "OPxFlasher", str(exc))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = self.session is not None and self.session.access.usable
        self.refresh_btn.setEnabled(not self.busy)
        self.device_combo.setEnabled(not self.busy)
        self.preview_btn.setEnabled(ready and not self.busy)
        self.start_btn.setEnabled(ready and not self.busy)
        self.cancel_btn.setEnabled(self.busy)
        self.open_btn.setEnabled(self.last_folder is not None)
        self.check_btn.setEnabled(not self.busy)
        if hasattr(self, "flash_btn"):
            self.flash_btn.setEnabled(not self.busy and self._can_offer_flash())
        if hasattr(self, "root_tab"):
            self.root_tab.refresh_buttons()
        if hasattr(self, "restore_btn"):
            ready = bool(self.sig_plan and self.sig_plan.ok) and self.sig_confirm.text().strip() == "RESTORE"
            self.restore_btn.setEnabled(not self.busy and ready)
        for b in self.mode_group.buttons():
            b.setEnabled(not self.busy)

    def _show_banner(self, text: str, level: str) -> None:
        colours = {"danger": "#5c1d1b", "warning": "#4d3b12", "info": "#1d3550"}
        self.banner.setStyleSheet(f"background:{colours.get(level, '#1d3550')}")
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def _invalidate_plan(self) -> None:
        self.plan = None
        self.table.setRowCount(0)

    # --- devices ----------------------------------------------------------

    def refresh_devices(self) -> None:
        self._set_busy(True)
        self.say("Looking for phones...")
        self.run_job(Job(dev.scan), self._devices_found)

    def _devices_found(self, scan: dev.Scan) -> None:
        self._set_busy(False)
        for err in scan.errors:
            self.say(err)
        tools_missing = any("not found" in e for e in scan.errors)
        self.tools_btn.setVisible(tools_missing)
        if tools_missing and not scan.devices:
            self._show_banner(
                "adb and fastboot are missing. Click \"Download adb and fastboot\" to get "
                "Google's official platform-tools, then plug in your phone.",
                "warning",
            )
            self._update_buttons()
            return
        self.devices = scan.devices
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self.devices:
            self.device_combo.addItem(d.label)
        self.device_combo.blockSignals(False)
        if not self.devices:
            self.session = None
            self._clear_info()
            self._show_banner(
                "No phone found. Enable USB debugging and accept the prompt, or boot a custom recovery.",
                "info",
            )
            self._update_buttons()
            return
        self._device_selected(0)

    def download_tools(self) -> None:
        answer = QMessageBox.question(
            self, "OPxFlasher",
            "Download Google's official Android platform-tools (adb and fastboot, about 10 MB)?\n\n"
            "They come straight from dl.google.com and Google's terms apply.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self.tools_btn.setEnabled(False)
        self.say("Downloading platform-tools from Google...")
        self.run_job(Job(fetch_platform_tools), self._tools_downloaded, self._tools_failed)

    def _tools_downloaded(self, path) -> None:
        self.tools_btn.setEnabled(True)
        self.tools_btn.hide()
        self.say(f"platform-tools saved to {path}")
        self.refresh_devices()

    def _tools_failed(self, exc: BaseException) -> None:
        self.tools_btn.setEnabled(True)
        self._job_failed(exc)

    def _clear_info(self) -> None:
        for lbl in self.info_labels.values():
            lbl.setText("-")

    def _device_selected(self, index: int) -> None:
        self.session = None
        self._invalidate_plan()
        self._clear_info()
        self._update_buttons()
        if index < 0 or index >= len(self.devices):
            return
        d = self.devices[index]
        if not d.can_backup:
            msg = {
                "unauthorized": "Unlock the phone and accept the USB debugging prompt, then press Refresh.",
                "bootloader": "The phone is in the bootloader. Backups need Android (rooted) or a custom recovery.",
                "fastbootd": "The phone is in fastbootd. Backups need Android (rooted) or a custom recovery.",
            }.get(d.state, f"Backups are not possible in '{d.mode_text}' mode.")
            self._show_banner(msg, "warning")
            return
        self._set_busy(True)
        self.say(f"Connecting to {d.serial}...")
        self.run_job(Job(open_session, d.serial), self._session_ready)

    def _session_ready(self, session: Session) -> None:
        self._set_busy(False)
        self.session = session
        facts = bk.device_facts(session.props)
        for key in ("model", "build", "android", "security_patch", "slot_suffix"):
            self.info_labels[key].setText(facts.get(key) or "-")
        access_text = {
            "su": "Yes (su: Magisk / KernelSU / APatch)",
            "root-shell": "Yes (recovery / adb root)",
            "none": "No",
        }[session.access.kind]
        self.info_labels["access"].setText(access_text)
        self.info_labels["profile"].setText(session.profile.name)

        issues = session.profile.issues_for(session.props)
        if issues:
            self._show_banner("\n\n".join(i.message for i in issues), issues[0].severity)
        elif not session.access.usable:
            self._show_banner(
                "No root access. Grant root to 'Shell' in Magisk/KernelSU/APatch, or boot a custom recovery, "
                "then press Refresh.",
                "warning",
            )
        else:
            self._show_banner("", "info")
        self.say(f"Connected: {facts.get('model')} {facts.get('build')} (root: {session.access.kind})")
        self._sig_device = None
        if not self.sig_backup_edit.text().strip() and self.last_folder:
            self.sig_backup_edit.setText(str(self.last_folder))
        self.refresh_signal()
        self._update_buttons()

    # --- backup -----------------------------------------------------------

    def preview(self, then=None) -> None:
        if not self.session:
            return
        self._set_busy(True)
        self._after_plan = then if callable(then) else None
        s = self.session
        self.run_job(Job(bk.build_plan, s.shell, s.profile, self.mode(), s.props), self._plan_ready)

    def _plan_ready(self, plan: bk.Plan) -> None:
        then, self._after_plan = getattr(self, "_after_plan", None), None
        self._set_busy(False)
        self.plan = plan
        self.table.setRowCount(len(plan.items))
        for row, item in enumerate(plan.items):
            for col, text in enumerate((item.name, human(item.size), item.path, "waiting")):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.say(f"{MODE_TEXT[plan.mode]}: {len(plan.items)} partition(s), {human(plan.total_bytes)} total")
        if plan.missing:
            self.say("Not found on this phone: " + ", ".join(plan.missing))
        for w in plan.warnings:
            self.say("Note: " + w)
        if then:
            then()

    def start_backup(self) -> None:
        if not self.session:
            return
        if not self.plan or self.plan.mode != self.mode():
            self.preview(then=self.start_backup)
            return
        out = Path(self.out_edit.text()).expanduser()
        self.cancel_flag = threading.Event()
        self._set_busy(True)
        self.overall_bar.setValue(0)
        self.item_bar.setValue(0)
        job = Job(
            bk.run_backup,
            self.session.shell,
            self.plan,
            out,
            verify_on_device=self.verify_box.isChecked(),
            cancel=self.cancel_flag,
        )
        job.kwargs["on_event"] = job.signals.event.emit
        self.run_job(job, self._backup_done, self._backup_failed, self._backup_event)

    def cancel(self) -> None:
        self.cancel_flag.set()
        self.say("Cancelling...")

    def _row_of(self, name: str) -> int:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == name:
                return row
        return -1

    def _backup_event(self, e: dict) -> None:
        t = e["type"]
        if t == "item_start":
            self.item_label.setText(f"[{e['index']}/{e['total']}] Reading {e['name']} ({human(e['size'])})")
            self.item_bar.setValue(0)
            row = self._row_of(e["name"])
            if row >= 0:
                self.table.item(row, 3).setText("reading...")
        elif t == "progress":
            if e.get("size"):
                self.item_bar.setValue(int(100 * e["done"] / e["size"]))
            if e.get("overall_total"):
                self.overall_bar.setValue(int(100 * e["overall_done"] / e["overall_total"]))
        elif t == "item_done":
            self.item_bar.setValue(100)
            row = self._row_of(e["name"])
            if row >= 0:
                label = {"ok": "verified", "unverified": "saved (not verified)", "failed": "FAILED"}[e["status"]]
                self.table.item(row, 3).setText(label + (f" - {e['error']}" if e["error"] else ""))
            self.say(f"{e['name']}: {e['status']} {e['error']}".strip())
        elif t == "log":
            self.say(e["message"])
        elif t == "finished":
            self.overall_bar.setValue(100)
            self.last_folder = Path(e["folder"])

    def _backup_done(self, folder: Path) -> None:
        self._set_busy(False)
        self.last_folder = Path(folder)
        if not self.sig_backup_edit.text().strip():
            self.sig_backup_edit.setText(str(folder))
        self.item_label.setText("Backup finished")
        self.say(f"Saved to {folder}")
        self._update_buttons()

    def _backup_failed(self, exc: BaseException) -> None:
        if isinstance(exc, bk.Cancelled):
            self._set_busy(False)
            self.item_label.setText("Cancelled")
            self.say("Backup cancelled. Finished images and the manifest were kept.")
            return
        self._job_failed(exc)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose backup folder", self.out_edit.text())
        if folder:
            self.out_edit.setText(folder)

    def _open_folder(self) -> None:
        if self.last_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_folder)))

    def verify_existing(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a backup folder (with manifest.json)")
        if not folder:
            return
        self._set_busy(True)
        self.run_job(Job(bk.verify_folder, Path(folder)), self._verify_done)

    def _verify_done(self, report) -> None:
        self._set_busy(False)
        bad = [f"{f}: {msg}" for f, ok, msg in report if not ok]
        self.say(f"Verified {len(report)} image(s): {len(report) - len(bad)} OK, {len(bad)} problem(s)")
        for line in bad:
            self.say("  " + line)
        if bad:
            QMessageBox.warning(self, "Verify backup", "Problems found:\n\n" + "\n".join(bad))
        else:
            QMessageBox.information(self, "Verify backup", f"All {len(report)} image(s) match the manifest.")

    # --- ROM check --------------------------------------------------------

    def _choose_rom_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose ROM", "", "Firmware (*.zip *.bin);;All files (*)")
        if path:
            self.rom_edit.setText(path)

    def _choose_rom_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder with payload.bin or .img files")
        if folder:
            self.rom_edit.setText(folder)

    def _choose_compare_backup(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose an OPxFlasher backup folder", self.out_edit.text())
        if folder:
            self.cmp_backup_edit.setText(folder)
            self.cmp_backup.setChecked(True)

    def check_rom(self) -> None:
        rom_path = self.rom_edit.text().strip()
        if not rom_path:
            QMessageBox.information(self, "ROM check", "Choose a ROM file or folder first.")
            return
        backup = self.cmp_backup_edit.text().strip() if self.cmp_backup.isChecked() else ""
        if self.cmp_backup.isChecked() and not backup:
            QMessageBox.information(self, "ROM check", "Choose the backup folder to compare with.")
            return
        self._set_busy(True)
        self.say(f"Checking {rom_path} ...")
        self.run_job(Job(run_rom_check, rom_path, backup, self.session), self._rom_report_ready)

    def _rom_report_ready(self, result) -> None:
        rom, device, report = result
        self.check_rom_obj, self.check_device = rom, device
        self._set_busy(False)
        self.report = report
        self.save_report_btn.setEnabled(True)
        colours = {"BLOCKED": "#5c1d1b", "OK WITH WARNINGS": "#4d3b12", "OK": "#1d4d2b"}
        text = {
            "BLOCKED": "BLOCKED - this ROM must not be flashed as things stand",
            "OK WITH WARNINGS": "OK with warnings - read them before going further",
            "OK": "OK - all safety checks passed (dry run)",
        }[report.verdict]
        self.verdict.setText(text)
        self.verdict.setStyleSheet(f"background:{colours[report.verdict]}")
        blocks = [c for c in report.checks if c.status == "block"]
        self.reasons.setText("\n".join(f"Stopped by {c.name}: {c.detail}" for c in blocks))
        self.reasons.setVisible(bool(blocks))
        r = report.rom
        self.rom_summary.setText(
            f"ROM: {r['product'] or '?'} / {r['codename'] or '?'}, Android {r['android'] or '?'}, "
            f"patch {r['security_patch'] or '?'}, full package: {r['full']}, {r['images']} images"
        )
        status_text = {"pass": "OK", "info": "Info", "warn": "Warning", "block": "STOP"}
        status_colour = {"pass": "#5fd38d", "info": "#9aa3b5", "warn": "#f0c24b", "block": "#ff6b63"}
        self.checks_table.setRowCount(len(report.checks))
        for row, c in enumerate(report.checks):
            cell = QTableWidgetItem(status_text[c.status])
            cell.setForeground(QColor(status_colour[c.status]))
            self.checks_table.setItem(row, 0, cell)
            self.checks_table.setItem(row, 1, QTableWidgetItem(c.name))
            self.checks_table.setItem(row, 2, QTableWidgetItem(c.detail))
        self.checks_table.resizeRowsToContents()
        action_text = {"flash": "flash", "super": "into super", "conditional": "optional", "skip": "never flashed"}
        self.plan_table.setRowCount(len(report.items))
        for row, i in enumerate(report.items):
            values = (i.partition, human(i.size), action_text[i.action], i.target, i.note)
            for col, v in enumerate(values):
                self.plan_table.setItem(row, col, QTableWidgetItem(v))
        self.say(f"ROM check: {report.verdict}")
        self._update_buttons()

    def _save_report(self) -> None:
        if not self.report:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save report", "opx-rom-report.json", "JSON (*.json)")
        if path:
            Path(path).write_text(self.report.to_json(), "utf-8")
            self.say(f"Report saved to {path}")


    # --- flashing -----------------------------------------------------------

    def _can_offer_flash(self) -> bool:
        if not self.report or not self.check_device or self.check_device.source != "live":
            return False
        return not fl.hard_blocks(self.report, [fl.OVERRIDE_QCOM_UNVALIDATED])

    def start_flash(self) -> None:
        if not self._can_offer_flash() or self.session is None:
            return
        hint = str(self.last_folder) if self.last_folder else self.cmp_backup_edit.text().strip()
        dialog = FlashDialog(self, self.check_rom_obj, self.check_device, self.report, hint)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.plan or not dialog.plan.ok:
            self.say("Flash cancelled. Nothing was written.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        work = Path(self.out_edit.text()).expanduser() / f"flash-{stamp}"
        work.mkdir(parents=True, exist_ok=True)
        self.flash_cancel = threading.Event()
        runner = fl.Runner(
            dialog.plan, self.check_rom_obj, self.session.profile, Tool("adb"), Tool("fastboot"),
            work / "journal.json", work / "images", cancel=self.flash_cancel,
        )
        self._launch(runner, work / "journal.json", self.flash_label, self.flash_bar, self.stop_flash_btn, "Flash")

    def _launch(self, runner, journal, label, bar, stop_btn, what: str) -> None:
        """Run a flash/restore Runner off the UI thread, showing progress in the given widgets."""
        self.flash_journal = journal
        self._active = (label, bar, stop_btn, what)
        self._set_busy(True)
        stop_btn.setEnabled(True)
        bar.setValue(0)
        bar.show()
        label.show()
        label.setText("Preparing...")
        self.say(f"{what} started. Journal: {journal}")
        job = Job(runner.run)
        runner.emit = job.signals.event.emit
        self.run_job(job, self._flash_done, self._flash_failed, self._flash_event)

    def _stop_flash(self) -> None:
        self.flash_cancel.set()
        for btn in (self.stop_flash_btn, self.stop_restore_btn, self.root_tab.stop_btn):
            btn.setEnabled(False)
        self.say("Will stop after the current step (a running write is never interrupted).")

    def _flash_event(self, e: dict) -> None:
        label, bar, _stop, _what = self._active
        t = e["type"]
        if t == "extract":
            label.setText(f"Preparing image {e['index']}/{e['total']}: {e['name']}")
        elif t == "step_start":
            label.setText(f"Step {e['index']}/{e['total']}: {e['label']}")
            bar.setValue(int(100 * (e["index"] - 1) / e["total"]))
            self.say(f"[{e['index']}/{e['total']}] {e['label']}")
        elif t == "step_done":
            self.say(f"    {e['status']}: {e['detail']}")
        elif t == "log":
            self.say(e["message"])
        elif t == "finished":
            bar.setValue(100)

    def _flash_done(self, plan) -> None:
        label, _bar, stop, what = self._active
        self._set_busy(False)
        stop.setEnabled(False)
        label.setText(f"{what} complete")
        self.say(f"{what} complete.")
        QMessageBox.information(self, what, f"{what} complete. The phone is rebooting." if plan.options.reboot_after
                                else f"{what} complete. The phone is still in the bootloader.")

    def _flash_failed(self, exc: BaseException) -> None:
        label, _bar, stop, what = self._active
        self._set_busy(False)
        stop.setEnabled(False)
        label.setText(f"{what} stopped")
        self.say(str(exc))
        QMessageBox.warning(
            self, f"{what} stopped",
            f"{exc}\n\nResume later with:\nopx flash --resume \"{getattr(self, 'flash_journal', '')}\"",
        )

    # --- signal / region tab ------------------------------------------------

    def _build_signal_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        intro = QLabel(
            "Lost mobile signal after an update on a converted phone? The update probably replaced the region "
            "files. This writes your phone's <b>own</b> oplusstanvbk and my_region back from a verified backup of "
            "this same phone. Nothing else is touched."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        box = QGroupBox("Backup to restore from")
        b_layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.sig_backup_edit = QLineEdit()
        self.sig_backup_edit.setPlaceholderText("A Critical or Full backup of this phone")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_signal_backup)
        row.addWidget(self.sig_backup_edit, 1)
        row.addWidget(browse)
        b_layout.addLayout(row)
        self.sig_table = make_table(["File", "Size", "Type", "Backup hash"], stretch=0)
        self.sig_table.setMaximumHeight(140)
        b_layout.addWidget(self.sig_table)
        self.sig_fastbootd = QCheckBox("Allow fastbootd (needed for my_region, which lives inside super)")
        self.sig_fastbootd.setChecked(True)
        b_layout.addWidget(self.sig_fastbootd)
        layout.addWidget(box)

        layout.addWidget(QLabel("What will happen:"))
        self.sig_preview = QPlainTextEdit()
        self.sig_preview.setReadOnly(True)
        layout.addWidget(self.sig_preview, 1)

        act = QHBoxLayout()
        act.addWidget(QLabel("Type RESTORE:"))
        self.sig_confirm = QLineEdit()
        self.sig_confirm.setMaximumWidth(160)
        act.addWidget(self.sig_confirm)
        self.restore_btn = QPushButton("Restore signal files")
        self.restore_btn.setObjectName("primary")
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self.start_restore)
        self.stop_restore_btn = QPushButton("Stop after this step")
        self.stop_restore_btn.setEnabled(False)
        self.stop_restore_btn.clicked.connect(self._stop_flash)
        act.addWidget(self.restore_btn)
        act.addWidget(self.stop_restore_btn)
        act.addStretch(1)
        layout.addLayout(act)
        self.sig_label = QLabel("")
        self.sig_label.hide()
        self.sig_bar = QProgressBar()
        self.sig_bar.setFormat("Restoring  %p%")
        self.sig_bar.hide()
        layout.addWidget(self.sig_label)
        layout.addWidget(self.sig_bar)

        self.sig_plan = None
        self._sig_device = None
        self._sig_hash_cache: dict[tuple[str, str], bool] = {}
        self.sig_backup_edit.textChanged.connect(self.refresh_signal)
        self.sig_fastbootd.toggled.connect(self.refresh_signal)
        self.sig_confirm.textChanged.connect(self.refresh_signal)
        return tab

    def _choose_signal_backup(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a backup of this phone", self.out_edit.text())
        if folder:
            self.sig_backup_edit.setText(folder)

    def _signal_device(self):
        if self.session is None or not self.session.access.usable:
            return None
        if self._sig_device is None or self._sig_device_serial != self.session.serial:
            self._sig_device = pn.device_from_session(self.session)
            self._sig_device_serial = self.session.serial
        return self._sig_device

    def refresh_signal(self) -> None:
        folder = self.sig_backup_edit.text().strip()
        profile = self.session.profile if self.session else None
        rows = []
        names = []
        if folder and profile:
            for name in profile.conditional:
                images = fl.backup_images(folder, name)
                if images:
                    names.append(name)
                for full, (path, sha, src) in sorted(images.items()):
                    key = (str(path), sha)
                    if key not in self._sig_hash_cache:
                        self._sig_hash_cache[key] = path.exists() and fl.sha256_file(path) == sha
                    rows.append((full, human(path.stat().st_size) if path.exists() else "missing",
                                 "logical" if src == "mapper" else "physical",
                                 "OK" if self._sig_hash_cache[key] else "MISMATCH"))
        self.sig_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, v in enumerate(values):
                self.sig_table.setItem(r, c, QTableWidgetItem(v))

        device = self._signal_device()
        if device is None:
            self.sig_plan = None
            self.sig_preview.setPlainText("Connect the phone in Android with root access (see the Phone panel).")
        elif not folder:
            self.sig_plan = None
            self.sig_preview.setPlainText("Choose the backup to restore from.")
        else:
            options = fl.FlashOptions(restore_from_backup=names, allow_fastbootd=self.sig_fastbootd.isChecked())
            self.sig_plan = fl.prepare_restore(device, Path(folder), options)
            lines = [f"{i:>2}. {st.label}" for i, st in enumerate(self.sig_plan.steps, 1)]
            if self.sig_plan.problems:
                lines += ["", "BLOCKED:"] + [f"  - {p}" for p in self.sig_plan.problems]
            else:
                lines += ["", "Ready. Type RESTORE to enable the button."]
            self.sig_preview.setPlainText("\n".join(lines))
        self._update_buttons()

    def start_restore(self) -> None:
        plan = self.sig_plan
        if not plan or not plan.ok or self.sig_confirm.text().strip() != "RESTORE" or self.session is None:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        work = Path(self.out_edit.text()).expanduser() / f"restore-{stamp}"
        work.mkdir(parents=True, exist_ok=True)
        self.flash_cancel = threading.Event()
        runner = fl.Runner(plan, None, self.session.profile, Tool("adb"), Tool("fastboot"),
                           work / "journal.json", work / "images", cancel=self.flash_cancel)
        self.sig_confirm.clear()
        self._launch(runner, work / "journal.json", self.sig_label, self.sig_bar, self.stop_restore_btn, "Restore")


def run_rom_check(rom_path: str, backup: str, session: Session | None):
    rom = open_rom(rom_path)
    if backup:
        device = pn.device_from_backup(Path(backup))
    elif session is not None and session.access.usable:
        device = pn.device_from_session(session)
    else:
        device = pn.no_device()
    return rom, device, pn.build_report(rom, device)


def main() -> int:
    set_windows_app_id()  # must run before the first window is created
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OPxFlasher")
    app.setDesktopFileName("opxflasher")
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    if "--smoke-test" in sys.argv:
        # Used by CI on the packaged .exe / AppImage: the app, its data files and
        # icon load, without touching any phone.
        app.processEvents()
        ok = not app.windowIcon().isNull() and win.windowTitle().startswith("OPxFlasher")
        print("smoke test", "ok" if ok else "FAILED", flush=True)
        return 0 if ok else 1
    win.show()
    win.refresh_devices()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
