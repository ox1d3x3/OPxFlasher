"""ROM check > Verify ROM...: is this ROM complete and intact, and what can install it?"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from opx.i18n import tr
from opx.rom import verify as vf

STATUS_TEXT = {vf.OK: "OK", vf.WARN: "Warning", vf.FAIL: "FAILED", vf.INFO: "Info"}
STATUS_COLOUR = {vf.OK: "#5fd38d", vf.WARN: "#f0c24b", vf.FAIL: "#ff6b63", vf.INFO: "#9aa3b5"}
TARGET_TEXT = {vf.YES: "Ready", vf.CONVERT: "After converting", vf.NO: "No"}
VERDICT_COLOUR = {"VERIFIED": "#1d4d2b", "VERIFIED WITH WARNINGS": "#4d3b12", "FAILED": "#5c1d1b"}


def human(n: int) -> str:
    return vf._size(n)


class VerifyDialog(QDialog):
    """`run_job(fn, done, failed, event=None)` as in ConvertDialog. When the user asks to
    convert afterwards, `convert_requested` is True and the dialog is accepted."""

    def __init__(self, parent, run_job, rom_path: str):
        super().__init__(parent)
        self.setWindowTitle(tr("Verify ROM"))
        self.resize(900, 620)
        self.run_job = run_job
        self.rom_path = Path(rom_path)
        self.report: vf.VerifyReport | None = None
        self.convert_requested = False
        self.cancel = threading.Event()
        self.busy = False

        layout = QVBoxLayout(self)
        intro = QLabel(tr("Checks that the ROM is complete and undamaged (every byte against the hashes the "
                          "package records, boot images against its vbmeta), and whether OPxFlasher and "
                          "OrangeFox / AERA can install it. Nothing is written."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(QLabel(f"<b>{self.rom_path.name}</b>"))

        self.deep = QCheckBox(tr("Full check: read every byte (takes a few minutes for a 9 GB ROM)"))
        self.deep.setChecked(True)
        layout.addWidget(self.deep)

        self.verdict = QLabel(tr("Press Verify to start."))
        self.verdict.setObjectName("verdict")
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Result"), tr("Check"), tr("Details")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        layout.addWidget(self.table, 1)

        self.targets = QLabel("")
        self.targets.setWordWrap(True)
        layout.addWidget(self.targets)

        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.save_btn = QPushButton(tr("Save report..."))
        self.save_btn.clicked.connect(self._save)
        self.convert_btn = QPushButton(tr("Convert..."))
        self.convert_btn.setToolTip(tr("Make an OPxFlasher image folder and/or an OrangeFox / AERA recovery zip"))
        self.convert_btn.clicked.connect(self._convert)
        self.verify_btn = QPushButton(tr("Verify"))
        self.verify_btn.setObjectName("primary")
        self.verify_btn.clicked.connect(self.start)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self._close_or_cancel)
        row.addWidget(self.save_btn)
        row.addStretch(1)
        row.addWidget(self.convert_btn)
        row.addWidget(self.verify_btn)
        row.addWidget(self.close_btn)
        layout.addLayout(row)
        self._update_buttons()

    # --- running -----------------------------------------------------------------

    def start(self) -> None:
        self.cancel.clear()
        self.busy = True
        self.report = None
        self.table.setRowCount(0)
        self.targets.setText("")
        self.verdict.setText(tr("Verifying..."))
        self.verdict.setStyleSheet("")
        self.progress.setRange(0, 0)
        self.progress.show()
        self._update_buttons()
        path, deep, cancel = self.rom_path, self.deep.isChecked(), self.cancel.is_set
        self.log(f"Verify: {path} ({'full' if deep else 'quick'} check)")
        self.run_job(
            lambda emit=None: vf.verify(path, deep=deep, progress=lambda w, d, t: emit((w, d, t)), cancel=cancel),
            self._done, self._failed, event=self._event)

    def _event(self, ev) -> None:
        what, done, total = ev
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done * 1000 / total))
            self.progress.setFormat(f"{what}: {human(done)} of {human(total)}")
        self.status.setText(tr("Checking") + f" {what}...")

    def _done(self, rep: vf.VerifyReport) -> None:
        self.busy = False
        self.report = rep
        self.progress.hide()
        self.status.setText("")
        self.verdict.setText({
            "VERIFIED": tr("VERIFIED - complete and undamaged"),
            "VERIFIED WITH WARNINGS": tr("VERIFIED with warnings - read them below"),
            "FAILED": tr("FAILED - don't flash this ROM"),
        }[rep.verdict] + ("" if rep.deep else "  " + tr("(quick check)")))
        self.verdict.setStyleSheet(f"background:{VERDICT_COLOUR[rep.verdict]}")
        shown = rep.sorted_findings()
        self.table.setRowCount(len(shown))
        for r, f in enumerate(shown):
            cell = QTableWidgetItem(STATUS_TEXT[f.status])
            cell.setForeground(QColor(STATUS_COLOUR[f.status]))
            self.table.setItem(r, 0, cell)
            self.table.setItem(r, 1, QTableWidgetItem(f.area))
            self.table.setItem(r, 2, QTableWidgetItem(f.text))
        self.table.resizeRowsToContents()
        self.targets.setText("\n".join(f"{t.name}: {TARGET_TEXT[t.status]} - {t.text}" for t in rep.targets))
        self.log(f"Verify: {rep.verdict}")
        for f in shown:
            if f.status in (vf.FAIL, vf.WARN):
                self.log(f"  {f.status.upper()} {f.area}: {f.text}")
        self._update_buttons()
        if rep.needs_conversion:
            self._offer_conversion(rep)

    def _failed(self, exc: BaseException) -> None:
        self.busy = False
        self.progress.hide()
        if isinstance(exc, vf.VerifyCancelled):
            self.verdict.setText(tr("Stopped."))
        else:
            self.verdict.setText(tr("Verify failed:") + f" {exc}")
            self.verdict.setStyleSheet(f"background:{VERDICT_COLOUR['FAILED']}")
        self.status.setText("")
        self._update_buttons()

    def _offer_conversion(self, rep: vf.VerifyReport) -> None:
        d = rep.detection
        names = [n for n, _s, _k in rep.images]
        listed = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
        answer = QMessageBox.question(
            self, tr("Convert this ROM?"),
            f"{d.label}: {len(names)} images ({human(d.total_size)}).\n\n{listed}\n\n"
            + tr("Converting makes an OPxFlasher image folder, and if you choose, a recovery zip for "
                 "OrangeFox / AERA. Convert now?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._convert()

    def _convert(self) -> None:
        self.convert_requested = True
        self.accept()

    # --- helpers -----------------------------------------------------------------

    def log(self, text: str) -> None:
        say = getattr(self.parent(), "say", None)
        if say:
            say(text)

    def _save(self) -> None:
        if not self.report:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save report"), "opx-verify-report.txt", "Text (*.txt)")
        if path:
            Path(path).write_text("\n".join(self.report.lines()) + "\n", "utf-8")
            self.log(f"Verify report saved to {path}")

    def _close_or_cancel(self) -> None:
        if self.busy:
            self.cancel.set()
            self.status.setText(tr("Stopping..."))
        else:
            self.reject()

    def _update_buttons(self) -> None:
        rep = self.report
        self.verify_btn.setEnabled(not self.busy)
        self.deep.setEnabled(not self.busy)
        self.save_btn.setEnabled(rep is not None and not self.busy)
        ok = rep is not None and rep.detection.ok and rep.detection.convertible[0] and not rep.failed
        self.convert_btn.setEnabled(not self.busy and ok)
        self.close_btn.setText(tr("Stop") if self.busy else tr("Close"))
