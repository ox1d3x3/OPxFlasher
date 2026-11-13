"""OPxFlasher desktop app (PySide6). Phase 1: read-only backup."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from opx import __version__, support
from opx import backup as bk
from opx import device as dev
from opx import flash as fl
from opx import plan as pn
from opx import reboot as rb
from opx.gui.apps_tab import AppsTab
from opx.gui.catalog_dialog import CatalogDialog
from opx.gui.diag_tab import DiagTab
from opx.gui.flash_dialog import FlashDialog
from opx.gui.icon import app_icon, set_windows_app_id
from opx.gui.image_tab import ImageTab
from opx.gui.root_tab import RootTab
from opx.i18n import tr
from opx.profile import MODE_TEXT, MODES
from opx.rom.ota import open_rom
from opx.session import Session, open_session
from opx.tools import Tool, fetch_platform_tools, missing_tools

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
QMenuBar { background: #15171c; border-bottom: 1px solid #2c3039; }
QMenuBar::item { padding: 4px 10px; background: transparent; }
QMenuBar::item:selected { background: #262a33; border-radius: 4px; }
QMenu { background: #1c1f26; border: 1px solid #2c3039; padding: 4px; }
QMenu::item { padding: 5px 24px 5px 20px; border-radius: 4px; }
QMenu::item:selected { background: #2f3440; }
QMenu::item:disabled { color: #6b7280; }
QMenu::separator { height: 1px; background: #2c3039; margin: 4px 8px; }
QLabel#statusText { font-weight: 600; }
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
        self.cancellable = False  # True only while a backup or recovery zip runs
        self.tools_ok = True
        self.polling = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.POLL_MS)
        self.poll_timer.timeout.connect(self.poll_devices)
        self._build_ui()
        self._update_buttons()

    # --- layout -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(10)

        from opx import about

        head = QHBoxLayout()
        title = QLabel(tr("OPxFlasher"))
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch(1)
        self.version_label = QLabel(
            f'<span style="color:#9aa3b5">v{escape(__version__)} · {escape(tr("by"))} '
            f'<a href="{about.DEVELOPER_URL}" style="color:#9aa3b5">{about.DEVELOPER} ({about.shown(about.HANDLE)})</a></span>')
        self.version_label.setOpenExternalLinks(True)
        self.version_label.setToolTip(tr("Help > About OPxFlasher for details"))
        head.addWidget(self.version_label, 0, Qt.AlignBottom)
        subtitle = QLabel(tr("AIO tool for OnePlus / OPPO devices: backup, check, flash, restore and rescue."))
        subtitle.setStyleSheet("color:#9aa3b5")
        layout.addLayout(head)
        layout.addWidget(subtitle)

        # device
        dev_box = QGroupBox(tr("Phone"))
        dev_layout = QVBoxLayout(dev_box)
        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._device_selected)
        self.refresh_btn = QPushButton(tr("Refresh"))
        self.refresh_btn.clicked.connect(self.refresh_devices)
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)
        dev_layout.addLayout(row)
        self.tools_btn = QPushButton(tr("Download adb and fastboot"))
        self.tools_btn.setToolTip(tr("Downloads Google's official platform-tools (Google's terms apply)"))
        self.tools_btn.clicked.connect(self.download_tools)
        self.tools_btn.hide()
        dev_layout.addWidget(self.tools_btn)
        status_row = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_text = QLabel()
        self.status_text.setObjectName("statusText")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_text, 1)
        dev_layout.insertLayout(0, status_row)
        help_row = QHBoxLayout()
        self.usb_btn = QPushButton(tr("Not showing up?"))
        self.usb_btn.setToolTip(tr("Phone not showing up? Check USB drivers (Windows) or permissions (Linux)"))
        self.usb_btn.clicked.connect(self.usb_check)
        self.rescue_btn = QPushButton(tr("Won't start?"))
        self.rescue_btn.setToolTip(tr("Phone won't start? See what state it's in (fastboot, recovery, 9008, ...) "
                                      "and what to try"))
        self.rescue_btn.clicked.connect(self.open_rescue)
        help_row.addWidget(self.usb_btn)
        help_row.addWidget(self.rescue_btn)
        dev_layout.addLayout(help_row)
        dev_layout.addWidget(QLabel(tr("Reboot to:")))
        reboot_row = QGridLayout()
        reboot_row.setHorizontalSpacing(4)
        self.reboot_btns: dict[str, QPushButton] = {}
        for i, (target, text) in enumerate((("system", tr("Android")), ("bootloader", tr("Bootloader")),
                                            ("fastbootd", tr("Fastbootd")), ("recovery", tr("Recovery")))):
            btn = QPushButton(text)
            btn.setStyleSheet("padding: 6px 4px")
            btn.clicked.connect(lambda _c=False, t=target: self.reboot_to(t))
            self.reboot_btns[target] = btn
            reboot_row.addWidget(btn, 0, i)
        dev_layout.addLayout(reboot_row)

        self.info_labels: dict[str, QLabel] = {}
        form = QFormLayout()
        for key, text in (
            ("model", tr("Model")),
            ("variant", tr("Variant")),
            ("build", tr("Build")),
            ("android", tr("Android")),
            ("security_patch", tr("Security patch")),
            ("slot_suffix", tr("Active slot")),
            ("bootloader", tr("Bootloader")),
            ("access", tr("Root access")),
            ("last_backup", tr("Last backup")),
            ("profile", tr("Profile")),
        ):
            lbl = QLabel(tr("-"))
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info_labels[key] = lbl
            form.addRow(text + ":", lbl)
        dev_layout.addLayout(form)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setOpenExternalLinks(False)
        self.banner.linkActivated.connect(self._banner_link)
        self.banner.hide()
        dev_layout.addWidget(self.banner)
        dev_box.setMinimumWidth(340)
        dev_box.setMaximumWidth(400)
        dev_layout.addStretch(1)

        # --- Backup tab ---------------------------------------------------
        backup_tab = QWidget()
        backup_layout = QVBoxLayout(backup_tab)
        opt_box = QGroupBox(tr("Backup"))
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
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        out_row.addWidget(QLabel(tr("Save to:")))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(browse)
        opt_layout.addLayout(out_row)
        self.verify_box = QCheckBox(tr("Verify every image on the phone with sha256sum (recommended)"))
        self.verify_box.setChecked(True)
        opt_layout.addWidget(self.verify_box)

        btn_row = QHBoxLayout()
        self.preview_btn = QPushButton(tr("Preview"))
        self.preview_btn.clicked.connect(self.preview)
        self.start_btn = QPushButton(tr("Start backup"))
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_backup)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton(tr("Open folder"))
        self.open_btn.clicked.connect(self._open_folder)
        self.verify_btn = QPushButton(tr("Verify a backup..."))
        self.verify_btn.clicked.connect(self.verify_existing)
        self.restore_rom_btn = QPushButton(tr("Restore full ROM..."))
        self.restore_rom_btn.setToolTip(tr("Put the whole OS back from a Full firmware backup of this phone"))
        self.restore_rom_btn.clicked.connect(self.restore_full_rom)
        self.restore_zip_btn = QPushButton(tr("Make recovery zip..."))
        self.restore_zip_btn.setToolTip(tr("Turn a Full firmware backup into a zip you can flash in AERA, OrangeFox or TWRP"))
        self.restore_zip_btn.clicked.connect(self.make_restore_zip)
        for b in (self.preview_btn, self.start_btn, self.cancel_btn, self.open_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(self.verify_btn)
        btn_row.addWidget(self.restore_rom_btn)
        btn_row.addWidget(self.restore_zip_btn)
        opt_layout.addLayout(btn_row)
        backup_layout.addWidget(opt_box)

        self.item_label = QLabel(tr("Idle"))
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
        rom_box = QGroupBox(tr("Firmware to check (nothing is flashed)"))
        rom_form = QVBoxLayout(rom_box)
        rom_row = QHBoxLayout()
        self.rom_edit = QLineEdit()
        self.rom_edit.setPlaceholderText("Full OTA zip, payload.bin, or a folder of .img files")
        pick_file = QPushButton(tr("File..."))
        pick_file.clicked.connect(self._choose_rom_file)
        pick_folder = QPushButton(tr("Folder..."))
        pick_folder.clicked.connect(self._choose_rom_folder)
        self.catalog_btn = QPushButton(tr("Official ROMs..."))
        self.catalog_btn.setToolTip("Pick an official ROM for the connected phone "
                                    "(OTA catalog by Daniel Springer)")
        self.catalog_btn.clicked.connect(self.open_catalog)
        rom_row.addWidget(QLabel(tr("ROM:")))
        rom_row.addWidget(self.rom_edit, 1)
        rom_row.addWidget(pick_file)
        rom_row.addWidget(pick_folder)
        rom_row.addWidget(self.catalog_btn)
        rom_form.addLayout(rom_row)

        cmp_row = QHBoxLayout()
        self.cmp_group = QButtonGroup(self)
        self.cmp_live = QRadioButton(tr("Connected phone"))
        self.cmp_live.setChecked(True)
        self.cmp_backup = QRadioButton(tr("Backup folder:"))
        self.cmp_group.addButton(self.cmp_live)
        self.cmp_group.addButton(self.cmp_backup)
        self.cmp_backup_edit = QLineEdit()
        self.cmp_backup_edit.setPlaceholderText("An OPxFlasher backup (Full mode gives the most checks)")
        pick_backup = QPushButton(tr("Browse..."))
        pick_backup.clicked.connect(self._choose_compare_backup)
        cmp_row.addWidget(QLabel(tr("Compare with:")))
        cmp_row.addWidget(self.cmp_live)
        cmp_row.addWidget(self.cmp_backup)
        cmp_row.addWidget(self.cmp_backup_edit, 1)
        cmp_row.addWidget(pick_backup)
        rom_form.addLayout(cmp_row)

        act_row = QHBoxLayout()
        self.check_btn = QPushButton(tr("Check ROM"))
        self.check_btn.setObjectName("primary")
        self.check_btn.clicked.connect(self.check_rom)
        self.save_report_btn = QPushButton(tr("Save report..."))
        self.save_report_btn.clicked.connect(self._save_report)
        self.save_report_btn.setEnabled(False)
        act_row.addWidget(self.check_btn)
        act_row.addWidget(self.save_report_btn)
        self.flash_btn = QPushButton(tr("Flash..."))
        self.flash_btn.setEnabled(False)
        self.flash_btn.setToolTip(tr("Writes to the phone. Needs a passing ROM check on the connected phone and a backup."))
        self.flash_btn.clicked.connect(self.start_flash)
        self.stop_flash_btn = QPushButton(tr("Stop after this step"))
        self.stop_flash_btn.setEnabled(False)
        self.stop_flash_btn.clicked.connect(self._stop_flash)
        act_row.addWidget(self.flash_btn)
        act_row.addWidget(self.stop_flash_btn)
        act_row.addStretch(1)
        rom_form.addLayout(act_row)
        rom_layout.addWidget(rom_box)

        self.verdict = QLabel(tr("Choose a ROM and press Check ROM."))
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
        self.tabs.addTab(backup_tab, tr("Backup"))
        self.rom_tab = rom_tab
        self.tabs.addTab(rom_tab, tr("ROM check"))
        self.tabs.addTab(self._build_signal_tab(), tr("Signal / region"))
        self.root_tab = RootTab(self)
        self.tabs.addTab(self.root_tab, tr("Root"))
        self.image_tab = ImageTab(self)
        self.tabs.addTab(self.image_tab, tr("Flash image"))
        self.apps_tab = AppsTab(self)
        self.tabs.addTab(self.apps_tab, tr("Apps"))
        self.diag_tab = DiagTab(self)
        self.tabs.addTab(self.diag_tab, tr("Diagnostics"))
        upper = QWidget()
        upper_layout = QHBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.addWidget(dev_box)
        upper_layout.addWidget(self.tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(tr("Log")))
        log_bar.addStretch(1)
        self.export_log_btn = QPushButton(tr("Export log..."))
        self.export_log_btn.setToolTip(tr("Save the log and phone details as a zip for a bug report (serials hidden)"))
        self.export_log_btn.clicked.connect(self.export_log)
        log_bar.addWidget(self.export_log_btn)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log)
        body = QSplitter(Qt.Vertical)
        body.addWidget(upper)
        body.addWidget(log_box)
        body.setSizes([660, 110])
        layout.addWidget(body, 1)

        self.setCentralWidget(root)
        self._build_menus()
        self._update_status(checking=True)

    def _build_menus(self) -> None:
        from opx import about, i18n

        bar = self.menuBar()

        def act(menu, text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(lambda _c=False: slot())
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setStatusTip(tip)
                a.setToolTip(tip)
            menu.addAction(a)
            return a

        def web(url):
            return lambda: QDesktopServices.openUrl(QUrl(url))

        file_menu = bar.addMenu(tr("&File"))
        act(file_menu, tr("Open backups folder"), self._open_backups_root)
        act(file_menu, tr("Verify a backup..."), self.verify_existing)
        file_menu.addSeparator()
        act(file_menu, tr("Export log..."), self.export_log, tip=tr("Save the log for a bug report (serials hidden)"))
        file_menu.addSeparator()
        act(file_menu, tr("Quit"), self.close, QKeySequence.Quit)

        phone = bar.addMenu(tr("&Phone"))
        self.refresh_act = act(phone, tr("Refresh"), self.refresh_devices, "F5")
        reboot_menu = phone.addMenu(tr("Reboot to"))
        self.reboot_acts = {}
        for target, text in (("system", tr("Android")), ("bootloader", tr("Bootloader")),
                             ("fastbootd", tr("Fastbootd")), ("recovery", tr("Recovery"))):
            self.reboot_acts[target] = act(reboot_menu, text, lambda t=target: self.reboot_to(t))
        phone.addSeparator()
        act(phone, tr("Phone not showing up?"), self.usb_check)
        act(phone, tr("Phone won't start?"), self.open_rescue)

        tools = bar.addMenu(tr("&Tools"))
        act(tools, tr("Download adb and fastboot"), self.download_tools)
        act(tools, tr("Official ROMs..."), self.open_catalog)
        act(tools, tr("Read a logs zip..."), lambda: self.diag_tab.read_logs())
        tools.addSeparator()
        lang = tools.addMenu(tr("Language"))
        self.lang_group = QActionGroup(self)
        self.lang_actions: dict[str, QAction] = {}
        for code, name in i18n.available().items():
            a = QAction(name, self, checkable=True)
            a.setChecked(code == i18n.language())
            a.triggered.connect(lambda _c=False, c=code, n=name: self.change_language(c, n))
            self.lang_group.addAction(a)
            lang.addAction(a)
            self.lang_actions[code] = a

        help_menu = bar.addMenu(tr("&Help"))
        act(help_menu, tr("User guide"), web(about.GUIDE_URL), QKeySequence.HelpContents)
        act(help_menu, tr("Command-line reference"), web(about.CLI_URL))
        act(help_menu, tr("Report a problem"), web(about.ISSUES_URL))
        help_menu.addSeparator()
        self.update_act = act(help_menu, tr("Check for updates"), self.check_updates)
        act(help_menu, tr("Releases page"), web(about.RELEASES_URL))
        help_menu.addSeparator()
        act(help_menu, tr("About OPxFlasher"), self.show_about)

    def show_about(self) -> None:
        from opx.gui.about_dialog import AboutDialog

        self.about_dialog = AboutDialog(self)
        self.about_dialog.open()

    def _open_backups_root(self) -> None:
        folder = Path(self.out_edit.text()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # --- helpers ----------------------------------------------------------

    def say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def open_rescue(self) -> None:
        from opx.gui.rescue_dialog import RescueDialog

        self.rescue_dialog = RescueDialog(self)
        self.rescue_dialog.show()
        self.rescue_dialog.check()

    def usb_check(self) -> None:
        from opx import usbcheck

        self.usb_btn.setEnabled(False)
        self.run_job(Job(usbcheck.check), self._usb_checked, self._usb_failed)

    def _usb_checked(self, findings) -> None:
        self.usb_btn.setEnabled(True)
        for f in findings:
            self.say(f"USB check: {f.message}")
        text = "\n\n".join(f.message for f in findings)
        actions = [a for f in findings for a in f.actions]
        if not actions:
            if any(f.level == "problem" for f in findings):
                QMessageBox.warning(self, "USB check", text)
            else:
                QMessageBox.information(self, "USB check", text)
            return
        labels = {"driver-page": tr("Get the driver")}
        box = QMessageBox(QMessageBox.Warning, tr("USB check"), text, parent=self)
        buttons = {box.addButton(labels[a], QMessageBox.ActionRole): a for a in dict.fromkeys(actions)}
        box.addButton(QMessageBox.Close)
        self.usb_box = box
        while True:  # stays open so several fixes can be tried; Close ends it
            box.exec()
            action = buttons.get(box.clickedButton())
            if not action:
                break
            self.usb_action(action)

    def usb_action(self, action: str) -> None:
        from opx import usbcheck

        if action == "driver-page":  # a web page only: OPxFlasher never opens Windows settings
            QDesktopServices.openUrl(QUrl(usbcheck.GOOGLE_DRIVER))
            self.say(f"Opened {usbcheck.GOOGLE_DRIVER}")

    def _usb_failed(self, exc: BaseException) -> None:
        self.usb_btn.setEnabled(True)
        self.say(f"USB check failed: {exc}")

    def export_log(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = Path(self.out_edit.text()).expanduser() / f"opxflasher-log-{stamp}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Save log", str(default), "Zip (*.zip)")
        if not path:
            return
        facts = bk.device_facts(self.session.props) if self.session else {}
        serials = [d.serial for d in self.devices]
        root = Path(self.out_edit.text()).expanduser()
        journals = sorted(root.glob("*/journal.json"), key=lambda p: p.stat().st_mtime)[-3:] if root.is_dir() else []
        out = support.export_log(Path(path), self.log.toPlainText(), facts, serials, journals)
        self.say(f"Log saved to {out} (serial numbers hidden). Attach it to a GitHub issue.")

    def check_updates(self) -> None:
        self.update_act.setEnabled(False)
        self.say("Checking GitHub for a newer release...")
        self.run_job(Job(support.check_for_update), self._update_checked, self._update_failed)

    def _update_checked(self, update) -> None:
        self.update_act.setEnabled(True)
        if update is None:
            self.say(f"You have the newest version ({__version__}).")
            return
        kind = "release candidate" if update.prerelease else "release"
        self.say(f"New {kind} available: {update.tag} - {update.url}")
        answer = QMessageBox.question(self, "Update available",
                                      f"OPxFlasher {update.tag} ({kind}) is available; you have {__version__}.\n\n"
                                      "Open the download page?")
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(update.url))

    def _update_failed(self, exc: BaseException) -> None:
        self.update_act.setEnabled(True)
        self.say(str(exc))

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

    def _set_busy(self, busy: bool, cancellable: bool = False) -> None:
        self.busy = busy
        self.cancellable = busy and cancellable
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = self.session is not None and self.session.access.usable
        self.refresh_btn.setEnabled(not self.busy)
        if hasattr(self, "refresh_act"):
            self.refresh_act.setEnabled(not self.busy)
        self.device_combo.setEnabled(not self.busy)
        self.preview_btn.setEnabled(ready and not self.busy)
        self.start_btn.setEnabled(ready and not self.busy)
        self.cancel_btn.setEnabled(self.cancellable)
        self.open_btn.setEnabled(self.last_folder is not None)
        self.check_btn.setEnabled(not self.busy)
        self.restore_rom_btn.setEnabled(not self.busy)
        self.restore_zip_btn.setEnabled(not self.busy)
        if hasattr(self, "flash_btn"):
            self.flash_btn.setEnabled(not self.busy and self._can_offer_flash())
        if hasattr(self, "root_tab"):
            self.root_tab.refresh_buttons()
        if hasattr(self, "image_tab"):
            self.image_tab.refresh_buttons()
        if hasattr(self, "apps_tab"):
            self.apps_tab.refresh_buttons()
        if hasattr(self, "diag_tab"):
            self.diag_tab.refresh_buttons()
        if hasattr(self, "reboot_btns"):
            for c in rb.choices(self.selected_device(), self._blocked_for_reboot()):
                btn = self.reboot_btns[c.target]
                btn.setEnabled(not self.busy and c.allowed)
                btn.setToolTip(c.reason or f"Reboot the phone to {c.label.lower()}")
                if hasattr(self, "reboot_acts"):  # the same rules for Phone > Reboot to
                    self.reboot_acts[c.target].setEnabled(btn.isEnabled())
                    self.reboot_acts[c.target].setToolTip(btn.toolTip())
        if hasattr(self, "restore_btn"):
            ready = bool(self.sig_plan and self.sig_plan.ok) and self.sig_confirm.text().strip() == "RESTORE"
            self.restore_btn.setEnabled(not self.busy and ready)
        for b in self.mode_group.buttons():
            b.setEnabled(not self.busy)

    def selected_device(self) -> dev.Device | None:
        i = self.device_combo.currentIndex()
        return self.devices[i] if 0 <= i < len(self.devices) else None

    def _blocked_for_reboot(self) -> set[str] | None:
        """Blocked actions for the phone's build; None when the build can't be read."""
        if self.session is not None:
            return self.session.profile.blocked_actions(self.session.props)
        d = self.selected_device()
        return None if d is None or d.transport == "fastboot" else set()

    def reboot_to(self, target: str) -> None:
        d = self.selected_device()
        blocked = self._blocked_for_reboot()
        choice = next(c for c in rb.choices(d, blocked) if c.target == target)
        if not choice.allowed:
            return
        if choice.confirm:
            answer = QMessageBox.warning(
                self, "Reboot", choice.reason + "\n\nReboot to fastbootd?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._set_busy(True)
        self.run_job(Job(rb.reboot, d, target, blocked), self._rebooted)

    def _rebooted(self, message: str) -> None:
        self._set_busy(False)
        self.say(message)
        self.session = None
        QTimer.singleShot(5000, self.refresh_devices)

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
        self._update_status(checking=True)
        self._set_busy(True)
        self.say("Looking for phones...")
        self.run_job(Job(dev.scan), self._devices_found)

    def _devices_found(self, scan: dev.Scan) -> None:
        self._set_busy(False)
        for err in scan.errors:
            self.say(err)
        tools_missing = any("not found" in e for e in scan.errors)
        self.tools_ok = not tools_missing
        self.tools_btn.setVisible(tools_missing)
        if tools_missing and not scan.devices:
            self.devices = []
            self._update_status()
            self._show_tools_banner()
            self._update_buttons()
            return
        if not self.poll_timer.isActive():
            self.poll_timer.start()
        self.devices = scan.devices
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self.devices:
            self.device_combo.addItem(d.label)
            self.device_combo.setItemData(self.device_combo.count() - 1, d.label, Qt.ToolTipRole)
        self.device_combo.blockSignals(False)
        self._update_status()
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

    # --- connection light ----------------------------------------------------

    POLL_MS = 3000
    STATUS_COLOURS = {"connected": "#3fb950", "waiting": "#d29922", "disconnected": "#f85149",
                      "checking": "#8b949e"}

    def connection_state(self) -> tuple[str, str]:
        """("connected" | "waiting" | "disconnected", text) for the light."""
        if not self.tools_ok:
            return "disconnected", tr("Disconnected: adb and fastboot are missing")
        if not self.devices:
            return "disconnected", tr("Disconnected: no phone found")
        d = self.selected_device() or self.devices[0]
        if d.transport == "fastboot" or d.state in ("device", "recovery", "sideload"):
            more = f" (+{len(self.devices) - 1})" if len(self.devices) > 1 else ""
            return "connected", f"{tr('Connected')}: {d.mode_text}{more}"
        if d.state == "unauthorized":
            return "waiting", tr("Waiting: allow USB debugging on the phone")
        return "waiting", tr("Waiting: the phone is offline, reconnect the cable")

    def _update_status(self, checking: bool = False) -> None:
        state, text = ("checking", tr("Looking for phones...")) if checking else self.connection_state()
        self.status_dot.setStyleSheet(f"color: {self.STATUS_COLOURS[state]}; font-size: 14pt")
        self.status_text.setText(text)
        self.status_text.setToolTip(text)
        self.status_dot.setProperty("state", state)

    def poll_devices(self) -> None:
        """Every few seconds, quietly: has a phone been plugged in or unplugged?"""
        if self.busy or self.polling or not self.tools_ok or QApplication.activeModalWidget() is not None:
            return
        self.polling = True
        self.run_job(Job(dev.quick_scan), self._polled, self._poll_failed)

    def _polled(self, seen) -> None:
        self.polling = False
        if seen is None:  # adb / fastboot went missing
            self.tools_ok = False
            self._update_status()
            return
        if seen != dev.presence(self.devices) and not self.busy and QApplication.activeModalWidget() is None:
            gone = {s for s, *_ in dev.presence(self.devices)} - {s for s, *_ in seen}
            if gone:
                self.say("Phone disconnected.")
            self.refresh_devices()

    def _poll_failed(self, exc: BaseException) -> None:
        self.polling = False  # a failed quick check is not worth a message; the next one retries

    # --- adb / fastboot ------------------------------------------------------

    TOOLS_LINK = "download-tools"

    def check_tools(self) -> None:
        """Start-up: are adb and fastboot there? If not, offer to download them."""
        self._show_banner(tr("Checking tool requirements..."), "info")
        self.say("Checking tool requirements...")
        missing = missing_tools()
        self.tools_ok = not missing
        if not missing:
            self.say("adb and fastboot found.")
            self._show_banner("", "info")
            self.refresh_devices()
            return
        self.say(f"Not found: {', '.join(missing)}")
        if self.ask_download_tools(missing):
            self.start_tools_download()
        else:
            self.say("Tools not downloaded. Use \"Click here to download\" in the phone panel whenever you're ready.")
            self.tools_btn.show()
            self._update_status()
            self._show_tools_banner()
            self._update_buttons()

    def ask_download_tools(self, missing: list[str]) -> bool:
        box = QMessageBox(QMessageBox.Question, tr("Tool requirements"),
                          tr("adb and fastboot aren't installed. OPxFlasher needs them to talk to your phone."),
                          parent=self)
        box.setInformativeText(tr(
            "Download Google's official Android platform-tools now? It's about 10 MB, comes straight "
            "from dl.google.com (Google's terms apply) and is saved for your user only."))
        download = box.addButton(tr("Download"), QMessageBox.AcceptRole)
        box.addButton(tr("Decline"), QMessageBox.RejectRole)
        box.setDefaultButton(download)
        box.exec()
        return box.clickedButton() is download

    def _show_tools_banner(self, problem: str = "") -> None:
        head = problem or tr("Tools not downloaded.")
        self._show_banner(
            f"<b>{escape(head)}</b> {escape(tr('adb and fastboot are needed to work with your phone.'))} "
            f'<a href="{self.TOOLS_LINK}" style="color:#ffd27a">{escape(tr("Click here to download"))}</a>',
            "danger")

    def _banner_link(self, link: str) -> None:
        if link == self.TOOLS_LINK:
            self.start_tools_download()

    def download_tools(self) -> None:
        """The 'Download adb and fastboot' button."""
        if self.ask_download_tools(missing_tools()):
            self.start_tools_download()

    def start_tools_download(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.tools_btn.setEnabled(False)
        self._show_banner(tr("Downloading adb and fastboot from Google..."), "info")
        self.say("Downloading platform-tools from Google...")
        dlg = QProgressDialog(tr("Downloading adb and fastboot from Google..."), "", 0, 100, self)
        dlg.setWindowTitle(tr("Tool requirements"))
        dlg.setCancelButton(None)  # the download is small and can't be stopped halfway safely
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        self.tools_progress = dlg
        job = Job(fetch_platform_tools)
        job.kwargs["progress"] = lambda done, total: job.signals.event.emit((done, total))
        self.run_job(job, self._tools_downloaded, self._tools_failed, self._tools_progress)

    def _tools_progress(self, ev) -> None:
        done, total = ev
        if total and getattr(self, "tools_progress", None):
            self.tools_progress.setValue(min(99, int(done * 100 / total)))

    def _close_tools_progress(self) -> None:
        dlg = getattr(self, "tools_progress", None)
        if dlg is not None:
            dlg.close()
            self.tools_progress = None

    def _tools_downloaded(self, path) -> None:
        self._close_tools_progress()
        self._set_busy(False)
        self.tools_btn.setEnabled(True)
        self.tools_btn.hide()
        self._show_banner("", "info")
        self.say(f"platform-tools saved to {path}")
        self.refresh_devices()

    def _tools_failed(self, exc: BaseException) -> None:
        self._close_tools_progress()
        self._set_busy(False)
        self.tools_btn.setEnabled(True)
        self.tools_btn.show()
        self.say(f"Download failed: {exc}")
        self._show_tools_banner(tr("Download failed."))
        QMessageBox.warning(self, tr("Tool requirements"),
                            f"{tr('Download failed.')}\n\n{exc}\n\n"
                            + tr("Check your internet connection and try again."))

    def _clear_info(self) -> None:
        for lbl in self.info_labels.values():
            lbl.setText("-")

    def _device_selected(self, index: int) -> None:
        self._update_status()
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
            "su": "Yes (su)",
            "root-shell": "Yes (recovery / adb root)",
            "none": "No",
        }[session.access.kind]
        self.info_labels["access"].setText(access_text)
        self.info_labels["profile"].setText(session.profile.name)
        self.info_labels["variant"].setText(session.profile.variant_name(facts.get("model", "")) or "-")
        self.info_labels["bootloader"].setText(support.bootloader_text(session.props))
        self._refresh_last_backup()

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
        self._set_busy(True, cancellable=True)
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
        if getattr(self, "zip_cancel", None) is not None:
            self.zip_cancel.set()
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

    def _refresh_last_backup(self) -> None:
        if self.session is None:
            return
        serial = self.session.props.get("ro.serialno", "")
        backups = support.find_backups(Path(self.out_edit.text()).expanduser(), serial)
        self.info_labels["last_backup"].setText(support.last_backup_text(backups))

    def _backup_done(self, folder: Path) -> None:
        self._set_busy(False)
        self.last_folder = Path(folder)
        self._refresh_last_backup()
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

    def _dialog_job(self, fn, done, failed, event=None) -> None:
        job = Job(fn)
        if event:
            job.kwargs["emit"] = job.signals.event.emit
        self.run_job(job, done, failed, event)

    def open_catalog(self) -> None:
        model = self.session.props.get("ro.product.model", "") if self.session else ""
        if not model:
            from PySide6.QtWidgets import QInputDialog

            # phone not in Android (fastboot, boot loop): ask for the model number
            text, ok = QInputDialog.getText(
                self, tr("Official ROMs"),
                tr("Phone model number (Settings > About phone, or the box), e.g. PJZ110 or CPH2653:"))
            model = text.strip().upper() if ok else ""
            if not model:
                return
        dlg = CatalogDialog(self, self._dialog_job, model)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.downloaded:
            self.rom_edit.setText(str(dlg.downloaded))
            self.say(f"Downloaded {dlg.downloaded}")
            self.check_rom()

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
        for c in report.checks:  # the reasons, so an exported log explains the verdict
            if c.status in (pn.BLOCK, pn.WARN):
                self.say(f"  {c.status.upper()} {c.name}: {c.detail}")
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

    def restore_full_rom(self) -> None:
        from opx import restore as rs

        d = self.selected_device()
        if self.session is None and (d is None or d.transport != "fastboot"):
            QMessageBox.information(self, "Restore full ROM",
                                    "Connect the phone in Android (USB debugging) or in fastboot first.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a Full firmware backup of this phone",
                                                  str(self.last_folder or self.out_edit.text()))
        if not folder:
            return
        folder = Path(folder)
        try:
            if self.session is not None:
                device, profile, live = pn.device_from_session(self.session), self.session.profile, True
            else:
                device = rs.device_in_fastboot(folder, d.serial)
                profile, live = device.profile, False
            older = any(c.name == "Older build" for c in rs.restore_checks(folder, device, live, False))
            rom, report, _ = rs.prepare_restore_rom(folder, device, live, older, [])
        except rs.RestoreError as e:
            QMessageBox.warning(self, "Restore full ROM", str(e))
            return
        dialog = FlashDialog(self, rom, device, report, str(folder))
        dialog.setWindowTitle(tr("Restore full ROM"))
        for name, cb in dialog.optional_boxes.items():
            cb.setChecked(rom.image(name) is not None)  # the phone's own region files
        if older:
            dialog.wipe_box.setChecked(True)
            dialog.wipe_box.setEnabled(False)
        dialog.refresh()
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.plan or not dialog.plan.ok:
            self.say("Restore cancelled. Nothing was written.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        work = folder.parent / f"restore-rom-{stamp}"
        work.mkdir(parents=True, exist_ok=True)
        self.flash_cancel = threading.Event()
        runner = fl.Runner(dialog.plan, rom, profile, Tool("adb"), Tool("fastboot"),
                           work / "journal.json", work / "images", cancel=self.flash_cancel)
        self.tabs.setCurrentWidget(self.rom_tab)  # progress shows under the ROM check
        self._launch(runner, work / "journal.json", self.flash_label, self.flash_bar, self.stop_flash_btn,
                     "Restore full ROM")

    def change_language(self, code: str, name: str = "") -> None:
        from opx import settings

        try:
            settings.put("language", code)
        except OSError as e:
            self.say(f"Couldn't save the language: {e}")
            return
        self.say(f"Language set to {name or code}. Restart OPxFlasher to apply it.")
        QMessageBox.information(self, tr("Language"), tr("Restart OPxFlasher to use the new language."))

    def ask_recovery(self) -> str | None:
        box = QMessageBox(self)
        box.setWindowTitle(tr("Make recovery zip"))
        box.setText(tr("Which recovery will you flash it in?"))
        box.setInformativeText(tr(
            "AERA: AERA's own image-zip install (it checks the model and anti-rollback).\n"
            "OrangeFox / TWRP: a normal flashable zip. It checks this is the same phone, the same "
            "slot and unchanged bootloader firmware, verifies every file, then restores."))
        aera = box.addButton("AERA", QMessageBox.AcceptRole)
        ofox = box.addButton(tr("OrangeFox / TWRP"), QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        return {aera: "aera", ofox: "orangefox"}.get(box.clickedButton())

    def make_restore_zip(self, recovery: str | None = None) -> None:
        from opx import restore_zip as rz

        recovery = recovery if isinstance(recovery, str) else self.ask_recovery()
        if not recovery:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a Full firmware backup",
                                                  str(self.last_folder or self.out_edit.text()))
        if not folder:
            return
        self.zip_recovery = recovery
        self.zip_cancel = threading.Event()
        self._set_busy(True, cancellable=True)
        self.item_label.setText("Making the recovery zip...")
        self.overall_bar.setValue(0)
        job = Job(lambda emit=None: rz.build(Path(folder), Path(folder).parent, progress=lambda w, d, t: emit((w, d, t)),
                                             cancel=self.zip_cancel.is_set, recovery=recovery))
        job.kwargs["emit"] = job.signals.event.emit
        self.run_job(job, self._zip_done, self._zip_failed, self._zip_progress)

    def _zip_progress(self, ev) -> None:
        what, done, total = ev
        self.item_label.setText(f"Recovery zip: {what}")
        self.overall_bar.setValue(int(done * 100 / total) if total else 100)

    def _zip_done(self, path) -> None:
        self._set_busy(False)
        self.overall_bar.setValue(100)
        self.item_label.setText("Recovery zip ready")
        self.say(f"Recovery zip saved: {path}")
        from opx import restore_zip as rz

        where = rz.RECOVERIES.get(getattr(self, "zip_recovery", "aera"), "AERA Recovery")
        QMessageBox.information(
            self, "Recovery zip",
            f"Saved {path.name} next to the backup.\n\nFlash it in {where} (Install). "
            "It's ONLY for the phone this backup came from, and it checks that before writing.")

    def _zip_failed(self, exc: BaseException) -> None:
        self._set_busy(False)
        self.item_label.setText("Idle")
        self.say(f"Recovery zip not made: {exc}")
        QMessageBox.warning(self, "Recovery zip", str(exc))

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
        for btn in (self.stop_flash_btn, self.stop_restore_btn, self.root_tab.stop_btn, self.image_tab.stop_btn):
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

        box = QGroupBox(tr("Backup to restore from"))
        b_layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.sig_backup_edit = QLineEdit()
        self.sig_backup_edit.setPlaceholderText("A Critical or Full backup of this phone")
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._choose_signal_backup)
        row.addWidget(self.sig_backup_edit, 1)
        row.addWidget(browse)
        b_layout.addLayout(row)
        self.sig_table = make_table(["File", "Size", "Type", "Backup hash"], stretch=0)
        self.sig_table.setMaximumHeight(140)
        b_layout.addWidget(self.sig_table)
        self.sig_fastbootd = QCheckBox(tr("Allow fastbootd (needed for my_region, which lives inside super)"))
        self.sig_fastbootd.setChecked(True)
        b_layout.addWidget(self.sig_fastbootd)
        layout.addWidget(box)

        layout.addWidget(QLabel(tr("What will happen:")))
        self.sig_preview = QPlainTextEdit()
        self.sig_preview.setReadOnly(True)
        layout.addWidget(self.sig_preview, 1)

        act = QHBoxLayout()
        act.addWidget(QLabel(tr("Type RESTORE:")))
        self.sig_confirm = QLineEdit()
        self.sig_confirm.setMaximumWidth(160)
        act.addWidget(self.sig_confirm)
        self.restore_btn = QPushButton(tr("Restore signal files"))
        self.restore_btn.setObjectName("primary")
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self.start_restore)
        self.stop_restore_btn = QPushButton(tr("Stop after this step"))
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
    from opx import i18n

    i18n.load()
    win = MainWindow()
    if "--smoke-test" in sys.argv:
        # Used by CI on the packaged .exe / AppImage: the app, its data files and
        # icon load, without touching any phone.
        app.processEvents()
        ok = not app.windowIcon().isNull() and win.windowTitle().startswith("OPxFlasher")
        print("smoke test", "ok" if ok else "FAILED", flush=True)
        return 0 if ok else 1
    win.show()
    QTimer.singleShot(0, win.check_tools)  # once the window is on screen
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
