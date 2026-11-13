"""Diagnostics tab: partition explorer and saving logs (both read-only)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opx.i18n import tr
from opx import bootlogs
from opx import explorer as ex


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


class DiagTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows: list[ex.PartitionRow] = []
        layout = QVBoxLayout(self)

        logs = QGroupBox(tr("Logs"))
        lv = QVBoxLayout(logs)
        text = QLabel(tr("Save the Android log, the kernel log and the log of the last crash or "
                         "unexpected reboot as one zip, for a bug report. Serial numbers are hidden. "
                         "The kernel and crash logs need root."))
        text.setWordWrap(True)
        text.setStyleSheet("color:#9aa3b5")
        lv.addWidget(text)
        row = QHBoxLayout()
        self.logs_btn = QPushButton(tr("Save phone logs..."))
        self.logs_btn.clicked.connect(self.save_logs)
        row.addWidget(self.logs_btn)
        row.addStretch(1)
        lv.addLayout(row)
        layout.addWidget(logs)

        parts = QGroupBox(tr("Partitions"))
        pv = QVBoxLayout(parts)
        row = QHBoxLayout()
        self.list_btn = QPushButton(tr("List partitions"))
        self.list_btn.setToolTip(tr("Every partition with its size and role (read-only, needs root)"))
        self.list_btn.clicked.connect(self.list_partitions)
        self.csv_btn = QPushButton(tr("Save as CSV..."))
        self.csv_btn.clicked.connect(self.save_csv)
        row.addWidget(self.list_btn)
        row.addWidget(self.csv_btn)
        row.addStretch(1)
        self.summary = QLabel("")
        row.addWidget(self.summary)
        pv.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Partition"), tr("Size"), tr("Type"), tr("Role")])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        pv.addWidget(self.table, 1)
        layout.addWidget(parts, 1)
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        live = self.win.session is not None and not self.win.busy
        self.logs_btn.setEnabled(live)
        self.list_btn.setEnabled(live and self.win.session.access.usable)
        self.csv_btn.setEnabled(bool(self.rows))

    # --- logs -------------------------------------------------------------------

    def save_logs(self, _=None, folder: str | None = None) -> None:
        from opx.gui.app import Job

        folder = folder or QFileDialog.getExistingDirectory(self, tr("Save logs in"), str(Path.home()))
        if not folder:
            return
        self.win._set_busy(True)
        job = Job(lambda emit=None: bootlogs.collect(self.win.session, Path(folder), emit=emit))
        job.kwargs["emit"] = job.signals.event.emit
        self.win.run_job(job, self._logs_saved, self._failed, lambda text: self.win.say(f"Logs: {text}..."))

    def _logs_saved(self, path) -> None:
        self.win._set_busy(False)
        self.win.say(f"Phone logs saved: {path}")
        QMessageBox.information(self, tr("Logs"), f"Saved {Path(path).name}.\n\nSerial numbers are hidden.")

    def _failed(self, exc: BaseException) -> None:
        self.win._set_busy(False)
        self.win.say(f"Diagnostics: {exc}")
        QMessageBox.warning(self, tr("Diagnostics"), str(exc))

    # --- partitions -------------------------------------------------------------

    def list_partitions(self) -> None:
        from opx.gui.app import Job

        s = self.win.session
        self.win._set_busy(True)
        self.win.run_job(Job(ex.explore, s.shell, s.profile, s.props), self._listed, self._failed)

    def _listed(self, rows) -> None:
        self.win._set_busy(False)
        self.rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            size = QTableWidgetItem(_human(r.size))
            for c, item in enumerate((QTableWidgetItem(r.name), size, QTableWidgetItem(r.kind), QTableWidgetItem(r.role))):
                self.table.setItem(i, c, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        total = sum(r.size or 0 for r in rows if r.kind == "physical")
        self.summary.setText(f"{len(rows)} partitions, {_human(total)} on the chip")
        self.refresh_buttons()

    def save_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Save partition list"), "partitions.csv", "CSV (*.csv)")
        if path:
            Path(path).write_text(ex.to_csv(self.rows), "utf-8")
            self.win.say(f"Partition list saved: {path}")
