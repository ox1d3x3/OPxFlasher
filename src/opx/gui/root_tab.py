"""The Root tab: a four-step assistant around the root manager app on the phone."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from opx import flash as fl
from opx import plan as pn
from opx import root as rt
from opx.i18n import tr
from opx.rom.ota import open_rom
from opx.tools import Tool

PATCH_HINT = {
    "magisk": "Open <b>Magisk</b> → Install → <b>Select and Patch a File</b> → choose <b>Download/{name}</b> → Let's Go.",
    "kernelsu": "Open <b>KernelSU</b> → Install → <b>Select a file</b> → choose <b>Download/{name}</b> (LKM mode) → Next.",
    "apatch": "Open <b>APatch</b> → Patch → <b>Select a boot image</b> → choose <b>Download/{name}</b>, set your superkey → Start.",
}


def prepare_on_phone(session_live, manager: str, rom_path: str, backup_path: str, work: Path) -> rt.RootSession:
    device = pn.device_from_session(session_live)
    partition = rt.MANAGERS[manager][1]
    if rom_path:
        stock = rt.stock_from_rom(open_rom(rom_path), partition, device.facts, work)
        source = f"ROM {Path(rom_path).name}"
    else:
        stock = rt.stock_from_backup(Path(backup_path), partition, device.facts, work)
        source = f"backup {Path(backup_path).name}"
    session = rt.new_session(manager, device, stock, source)
    rt.push_stock(session_live.adb, session)
    session.save(work)
    return session


def pull_from_phone(adb: Tool, session: rt.RootSession, work: Path) -> list[str]:
    problems = rt.pull_patched(adb, session, work)
    session.save(work)
    return problems


class RootTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.session: rt.RootSession | None = None
        self.work: Path | None = None
        self.pull_problems: list[str] | None = None
        layout = QVBoxLayout(self)

        # 1. root app
        g1 = QGroupBox(tr("1. Your root app"))
        l1 = QHBoxLayout(g1)
        self.manager_group = QButtonGroup(self)
        for key, (name, part) in rt.MANAGERS.items():
            rb = QRadioButton(f"{name}  ({part})")
            rb.setProperty("manager", key)
            rb.setChecked(key == "magisk")
            self.manager_group.addButton(rb)
            l1.addWidget(rb)
        l1.addStretch(1)
        layout.addWidget(g1)

        # 2. stock image
        g2 = QGroupBox(tr("2. Stock image of the build the phone is running now"))
        l2 = QVBoxLayout(g2)
        self.src_group = QButtonGroup(self)
        self.src_rom = QRadioButton(tr("Full OTA of this build:"))
        self.src_rom.setChecked(True)
        self.src_backup = QRadioButton(tr("Full backup made on this build:"))
        self.src_group.addButton(self.src_rom)
        self.src_group.addButton(self.src_backup)
        self.rom_edit, self.backup_src_edit = QLineEdit(), QLineEdit()
        for radio, edit, picker in ((self.src_rom, self.rom_edit, self._pick_rom), (self.src_backup, self.backup_src_edit, self._pick_backup_src)):
            row = QHBoxLayout()
            row.addWidget(radio)
            row.addWidget(edit, 1)
            b = QPushButton(tr("Browse..."))
            b.clicked.connect(picker)
            row.addWidget(b)
            l2.addLayout(row)
        row = QHBoxLayout()
        self.prepare_btn = QPushButton(tr("Check and send to phone"))
        self.prepare_btn.clicked.connect(self.prepare)
        row.addWidget(self.prepare_btn)
        row.addStretch(1)
        l2.addLayout(row)
        self.prepare_status = QLabel("")
        self.prepare_status.setWordWrap(True)
        l2.addWidget(self.prepare_status)
        layout.addWidget(g2)

        # 3. patch on the phone
        g3 = QGroupBox(tr("3. Patch it on the phone"))
        l3 = QVBoxLayout(g3)
        self.hint = QLabel(tr("Send the stock image first."))
        self.hint.setWordWrap(True)
        l3.addWidget(self.hint)
        row = QHBoxLayout()
        self.pull_btn = QPushButton(tr("I've patched it - find and check it"))
        self.pull_btn.clicked.connect(self.pull)
        row.addWidget(self.pull_btn)
        row.addStretch(1)
        l3.addLayout(row)
        self.pull_status = QLabel("")
        self.pull_status.setWordWrap(True)
        l3.addWidget(self.pull_status)
        layout.addWidget(g3)

        # 4. flash
        g4 = QGroupBox(tr("4. Flash (current slot only)"))
        l4 = QVBoxLayout(g4)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Your backup:")))
        self.backup_edit = QLineEdit()
        self.backup_edit.setPlaceholderText("Critical or Full backup of this phone (required)")
        b = QPushButton(tr("Browse..."))
        b.clicked.connect(self._pick_backup)
        row.addWidget(self.backup_edit, 1)
        row.addWidget(b)
        l4.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Type ROOT or UNROOT:")))
        self.confirm = QLineEdit()
        self.confirm.setMaximumWidth(140)
        row.addWidget(self.confirm)
        self.root_btn = QPushButton(tr("Flash patched image"))
        self.root_btn.setObjectName("primary")
        self.root_btn.clicked.connect(lambda: self.flash("root"))
        self.unroot_btn = QPushButton(tr("Restore stock image"))
        self.unroot_btn.clicked.connect(lambda: self.flash("unroot"))
        self.stop_btn = QPushButton(tr("Stop after this step"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(win._stop_flash)
        for w in (self.root_btn, self.unroot_btn, self.stop_btn):
            row.addWidget(w)
        row.addStretch(1)
        l4.addLayout(row)
        self.label = QLabel("")
        self.label.hide()
        self.bar = QProgressBar()
        self.bar.setFormat("%p%")
        self.bar.hide()
        l4.addWidget(self.label)
        l4.addWidget(self.bar)
        layout.addWidget(g4)
        layout.addStretch(1)

        self.confirm.textChanged.connect(self.refresh_buttons)
        self.manager_group.buttonToggled.connect(lambda *_: self._reset())
        self.refresh_buttons()

    # --- helpers --------------------------------------------------------

    def manager(self) -> str:
        return self.manager_group.checkedButton().property("manager")

    def _reset(self) -> None:
        self.session = None
        self.pull_problems = None
        self.prepare_status.setText("")
        self.pull_status.setText("")
        self.hint.setText("Send the stock image first.")
        self.refresh_buttons()

    def _pick_rom(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Full OTA of the current build", "", "OTA (*.zip);;All files (*)")
        if path:
            self.rom_edit.setText(path)
            self.src_rom.setChecked(True)

    def _pick_backup_src(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Full backup made on this build")
        if folder:
            self.backup_src_edit.setText(folder)
            self.src_backup.setChecked(True)

    def _pick_backup(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Your backup of this phone")
        if folder:
            self.backup_edit.setText(folder)

    def _status(self, label: QLabel, ok: bool, text: str) -> None:
        label.setStyleSheet(f"color:{'#5fd38d' if ok else '#ff8a80'}")
        label.setText(text)

    def refresh_buttons(self) -> None:
        win = self.win
        live = win.session is not None and not win.busy
        self.prepare_btn.setEnabled(live)
        self.pull_btn.setEnabled(live and self.session is not None)
        word = self.confirm.text().strip()
        patched_ok = self.session is not None and self.pull_problems == []
        self.root_btn.setEnabled(live and patched_ok and word == "ROOT")
        self.unroot_btn.setEnabled(live and self.session is not None and word == "UNROOT")
        if not self.backup_edit.text().strip() and win.last_folder:
            self.backup_edit.setText(str(win.last_folder))

    # --- actions --------------------------------------------------------

    def prepare(self) -> None:
        win = self.win
        if win.session is None:
            return
        rom = self.rom_edit.text().strip() if self.src_rom.isChecked() else ""
        backup = self.backup_src_edit.text().strip() if self.src_backup.isChecked() else ""
        if not rom and not backup:
            QMessageBox.information(self, "Root", "Choose the full OTA (or Full backup) of the build the phone runs now.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.work = Path(win.out_edit.text()).expanduser() / f"root-{stamp}"
        self._reset()
        win._set_busy(True)
        win.say("Preparing the stock image...")
        from opx.gui.app import Job

        win.run_job(Job(prepare_on_phone, win.session, self.manager(), rom, backup, self.work), self._prepared, self._prepare_failed)

    def _prepared(self, session: rt.RootSession) -> None:
        self.win._set_busy(False)
        self.session = session
        name = Path(session.remote_stock).name
        self._status(self.prepare_status, True,
                     f"Stock {session.partition} matches build {session.device.get('build')} ({session.source}). "
                     f"Sent to {session.remote_stock}.")
        self.hint.setText(PATCH_HINT[session.manager].format(name=name) + "<br>Then press the button below.")
        self.win.say(f"Stock {session.partition} sent to {session.remote_stock}")
        self.win._update_buttons()

    def _prepare_failed(self, exc: BaseException) -> None:
        self.win._set_busy(False)
        self._status(self.prepare_status, False, str(exc))
        self.win.say(f"Root: {exc}")
        self.win._update_buttons()

    def pull(self) -> None:
        if not self.session or self.win.session is None:
            return
        from opx.gui.app import Job

        self.win._set_busy(True)
        self.win.run_job(Job(pull_from_phone, self.win.session.adb, self.session, self.work), self._pulled, self._pull_failed)

    def _pulled(self, problems: list[str]) -> None:
        self.win._set_busy(False)
        self.pull_problems = problems
        if problems:
            self._status(self.pull_status, False, "The patched image doesn't look right:\n- " + "\n- ".join(problems))
        else:
            self._status(self.pull_status, True,
                         f"Found {Path(self.session.patched_remote).name}: valid boot image, same format as stock, "
                         "and actually patched.")
        self.win._update_buttons()

    def _pull_failed(self, exc: BaseException) -> None:
        self.win._set_busy(False)
        self.pull_problems = None
        self._status(self.pull_status, False, str(exc))
        self.win._update_buttons()

    def flash(self, purpose: str) -> None:
        win = self.win
        if not self.session or win.session is None:
            return
        device = pn.device_from_session(win.session)
        backup = self.backup_edit.text().strip()
        plan = rt.prepare_flash(device, Path(backup) if backup else None, self.session, purpose)
        if not plan.ok:
            QMessageBox.warning(self, "Root", "Blocked:\n\n- " + "\n- ".join(plan.problems))
            return
        win.flash_cancel = threading.Event()
        journal = self.work / f"{purpose}-journal.json"
        runner = fl.Runner(plan, None, win.session.profile, Tool("adb"), Tool("fastboot"), journal,
                           self.work / "images", cancel=win.flash_cancel)
        self.confirm.clear()
        win._launch(runner, journal, self.label, self.bar, self.stop_btn, "Root" if purpose == "root" else "Unroot")
