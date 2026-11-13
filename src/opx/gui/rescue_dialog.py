"""'Phone won't start?': shows what state the phone is in and what to try."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opx import rescue
from opx.i18n import tr


class RescueDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(tr("Phone won't start?"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.title = QLabel(tr("Looking at the phone..."))
        self.title.setStyleSheet("font-size:15px; font-weight:600")
        layout.addWidget(self.title)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#9aa3b5")
        layout.addWidget(self.summary)
        self.steps_box = QWidget()
        self.steps = QGridLayout(self.steps_box)
        self.steps.setColumnStretch(1, 1)
        layout.addWidget(self.steps_box)
        row = QHBoxLayout()
        row.addStretch(1)
        self.again_btn = QPushButton(tr("Check again"))
        self.again_btn.clicked.connect(self.check)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        row.addWidget(self.again_btn)
        row.addWidget(close)
        layout.addLayout(row)
        self.assessment: rescue.Assessment | None = None
        self.buttons: dict[str, QPushButton] = {}

    def check(self) -> None:
        from opx.gui.app import Job

        self.again_btn.setEnabled(False)
        self.title.setText(tr("Looking at the phone..."))
        roots = [Path(self.win.out_edit.text())]
        self.win.run_job(Job(rescue.gather, roots), self._assessed, self._failed)

    def _failed(self, exc: BaseException) -> None:
        self.again_btn.setEnabled(True)
        self.title.setText(tr("Couldn't look at the phone"))
        self.summary.setText(str(exc))

    def _assessed(self, a: rescue.Assessment) -> None:
        self.again_btn.setEnabled(True)
        self.assessment = a
        self.title.setText(a.title)
        self.summary.setText(a.summary)
        while self.steps.count():
            item = self.steps.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.buttons = {}
        for i, step in enumerate(a.steps):
            num = QLabel(f"{i + 1}.")
            num.setStyleSheet("color:#9aa3b5")
            text = QLabel(step.text)
            text.setWordWrap(True)
            self.steps.addWidget(num, i, 0)
            self.steps.addWidget(text, i, 1)
            if step.action:
                btn = QPushButton(step.label or tr("Do it"))
                btn.clicked.connect(lambda _=False, act=step.action: self.run(act))
                self.steps.addWidget(btn, i, 2)
                self.buttons[step.action] = btn
        self.win.say(f"Rescue: {a.title}")
        self.win.refresh_devices()  # so reboot / restore act on what's connected now

    def run(self, action: str) -> None:
        win = self.win
        if action == "check-again":
            self.check()
        elif action == "usb-check":
            win.usb_check()
        elif action == "identify-9008":
            self.identify()
        elif action.startswith("reboot:"):
            win.reboot_to(action.split(":", 1)[1])
        elif action == "restore-rom":
            win.restore_full_rom()
        elif action == "restore-zip":
            win.make_restore_zip()
        elif action == "official-roms":
            win.open_catalog()
        elif action == "save-logs":
            win.diag_tab.save_logs()

    def identify(self) -> None:
        from opx import sahara
        from opx.gui.app import Job

        btn = self.buttons.get("identify-9008")
        if btn:
            btn.setEnabled(False)
        self.win.run_job(Job(sahara.identify_first), self._identified, self._identify_failed)

    def _identified(self, info) -> None:
        if "identify-9008" in self.buttons:
            self.buttons["identify-9008"].setEnabled(True)
        for line in info.lines():
            self.win.say(f"9008: {line}")
        QMessageBox.information(self, tr("Phone in 9008 mode"), "\n".join(info.lines()))

    def _identify_failed(self, exc: BaseException) -> None:
        if "identify-9008" in self.buttons:
            self.buttons["identify-9008"].setEnabled(True)
        self.win.say(f"9008 identify: {exc}")
        QMessageBox.warning(self, tr("Phone in 9008 mode"), str(exc))
