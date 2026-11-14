"""Tools > Unlock bootloader...: the warnings, a 30-second read, a small sum, the
owner's agreement, then one last "Are you sure?". Only then is anything sent."""

from __future__ import annotations

import random

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from opx import unlock as ul
from opx.i18n import tr

READ_SECONDS = 30


class UnlockDialog(QDialog):
    def __init__(self, parent, seconds: int = READ_SECONDS, rng: random.Random | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Unlock bootloader"))
        self.resize(640, 560)
        rng = rng or random.SystemRandom()
        self.left = seconds

        layout = QVBoxLayout(self)
        head = QLabel(tr("Read this carefully before you continue."))
        head.setObjectName("verdict")
        head.setStyleSheet("background:#5c1d1b")
        layout.addWidget(head)
        for text in ul.WARNINGS:
            w = QLabel("• " + tr(text))
            w.setWordWrap(True)
            layout.addWidget(w)
        before = QLabel(tr("Before you start: back up your photos and files, and turn on OEM unlocking and USB "
                           "debugging in Developer options (Settings > About phone: tap the build number 7 times "
                           "to show Developer options). OPxFlasher checks this and tells you if it's off."))
        before.setWordWrap(True)
        before.setStyleSheet("color:#f0c24b")
        layout.addWidget(before)

        self.timer_label = QLabel()
        layout.addWidget(self.timer_label)

        self.a, self.b = rng.randint(11, 49), rng.randint(11, 49)
        sum_row = QHBoxLayout()
        sum_row.addWidget(QLabel(tr("To show you've read this, what is") + f" {self.a} + {self.b}?"))
        self.answer = QLineEdit()
        self.answer.setMaximumWidth(90)
        self.answer.textChanged.connect(self._update)
        sum_row.addWidget(self.answer)
        sum_row.addStretch(1)
        layout.addLayout(sum_row)

        self.agree = QCheckBox(tr("I understand the risks of my action. Unlocking erases my phone, and the "
                                  "OPxFlasher developer is not responsible for any damage."))
        self.agree.toggled.connect(self._update)
        layout.addWidget(self.agree)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.go = QPushButton(tr("Unlock bootloader..."))
        self.go.setObjectName("danger")
        self.go.clicked.connect(self._confirm)
        row.addWidget(cancel)
        row.addWidget(self.go)
        layout.addLayout(row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._update()

    def _tick(self) -> None:
        self.left = max(0, self.left - 1)
        if self.left == 0:
            self.timer.stop()
        self._update()

    @property
    def sum_ok(self) -> bool:
        return self.answer.text().strip() == str(self.a + self.b)

    def _update(self) -> None:
        self.timer_label.setText(tr("Please read the warnings:") + f" {self.left} s" if self.left
                                 else tr("Thanks for reading."))
        self.go.setEnabled(self.left == 0 and self.sum_ok and self.agree.isChecked())

    def _confirm(self) -> None:
        if not self.go.isEnabled():
            return
        answer = QMessageBox.warning(
            self, tr("Are you sure?"),
            tr("Unlock the bootloader now? Everything on the phone will be erased and can't be recovered."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()
