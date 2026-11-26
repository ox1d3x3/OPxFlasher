"""Anti-rollback OPxFlasher can't check: the owner's own decision. What can go wrong, a minute to read
it, a small sum, the agreement, then "Are you sure?". Shows the Validate result for this phone and
ROM (and can run it), and refuses outright when Validate found the ROM is older than the phone."""

from __future__ import annotations

import random
from collections.abc import Callable

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

from opx import arb
from opx import flash as fl
from opx.i18n import tr

READ_SECONDS = 60


class ArbRiskDialog(QDialog):
    def __init__(self, parent, details: list[str], seconds: int = READ_SECONDS, rng: random.Random | None = None,
                 validation: arb.Validation | None = None,
                 validate: Callable[[], arb.Validation | None] | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Anti-rollback: accept the risk"))
        self.resize(700, 660)
        rng = rng or random.SystemRandom()
        self.left = seconds
        self.validation = validation
        self._validate = validate

        layout = QVBoxLayout(self)
        head = QLabel(tr("OPxFlasher can't check anti-rollback for this flash. Read this carefully."))
        head.setObjectName("verdict")
        head.setStyleSheet("background:#5c1d1b")
        head.setWordWrap(True)
        layout.addWidget(head)
        for text in fl.ARB_WARNINGS:
            w = QLabel("• " + tr(text))
            w.setWordWrap(True)
            layout.addWidget(w)
        for d in details:
            lb = QLabel(d)
            lb.setWordWrap(True)
            lb.setStyleSheet("color:#9aa3b5")
            layout.addWidget(lb)

        v_row = QHBoxLayout()
        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        v_row.addWidget(self.validation_label, 1)
        self.validate_btn = QPushButton(tr("Validate ROM / ARB"))
        self.validate_btn.setToolTip(tr("With the phone running Android: compare this ROM with the installed build"))
        self.validate_btn.clicked.connect(self._run_validate)
        self.validate_btn.setEnabled(validate is not None)
        v_row.addWidget(self.validate_btn)
        layout.addLayout(v_row)

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

        self.agree = QCheckBox(tr("I accept the risk. I have made sure this ROM is the same or a newer build of my "
                                  "phone, and the OPxFlasher developer is not responsible for any damage."))
        self.agree.toggled.connect(self._update)
        layout.addWidget(self.agree)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.go = QPushButton(tr("Accept the risk"))
        self.go.setObjectName("danger")
        self.go.clicked.connect(self._confirm)
        row.addWidget(cancel)
        row.addWidget(self.go)
        layout.addLayout(row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._show_validation()
        self._update()

    def _run_validate(self) -> None:
        if self._validate is None:
            return
        v = self._validate()
        if v is not None:
            self.validation = v
        self._show_validation()
        self._update()

    def _show_validation(self) -> None:
        v = self.validation
        if v is None:
            text, colour = (tr("Not validated. Start the phone in Android (USB debugging on), then click Validate "
                               "ROM / ARB to compare this ROM with the installed build."), "#f0c24b")
        else:
            when = f"  ({v.checked_utc[:16].replace('T', ' ')} UTC)" if v.checked_utc else ""
            text = tr("Validated:") + " " + v.detail + when
            colour = {arb.NEWER: "#5fd38d", arb.SAME: "#5fd38d", arb.OLDER: "#ff6b63"}.get(v.result, "#f0c24b")
        self.validation_label.setText(text)
        self.validation_label.setStyleSheet(f"color:{colour}")

    def _tick(self) -> None:
        self.left = max(0, self.left - 1)
        if self.left == 0:
            self.timer.stop()
        self._update()

    @property
    def sum_ok(self) -> bool:
        return self.answer.text().strip() == str(self.a + self.b)

    @property
    def refused(self) -> bool:
        """Validate found the ROM is older than the installed build: no way through here."""
        return self.validation is not None and self.validation.result == arb.OLDER

    def _update(self) -> None:
        self.timer_label.setText(tr("Please read the warnings:") + f" {self.left} s" if self.left
                                 else tr("Thanks for reading."))
        self.go.setEnabled(self.left == 0 and self.sum_ok and self.agree.isChecked() and not self.refused)

    def _confirm(self) -> None:
        if not self.go.isEnabled():
            return
        answer = QMessageBox.warning(
            self, tr("Are you sure?"),
            tr("Flash firmware whose anti-rollback version OPxFlasher couldn't check? If it's lower than the "
               "phone's, the phone can be bricked for good."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()
