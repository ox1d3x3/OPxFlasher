"""Pick and download an official ROM from Daniel Springer's OTA catalog."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
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

from opx import catalog
from opx.i18n import tr

COLUMNS = ("Version", "Region", "Model", "Security patch", "Built", "Size", "")


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "OPxFlasher"


class CatalogDialog(QDialog):
    """`run_job(fn, done, failed, event=None)` runs `fn` off the UI thread (the main
    window's job runner); with `event`, `fn` gets an `emit` keyword for progress.
    After a successful download, `downloaded` holds the zip."""

    def __init__(self, parent, run_job, model: str):
        super().__init__(parent)
        self.setWindowTitle(tr("Official ROMs"))
        self.resize(820, 480)
        self.run_job = run_job
        self.model = model
        self.releases: list[catalog.Release] = []
        self.downloaded: Path | None = None
        self.cancel = threading.Event()

        layout = QVBoxLayout(self)
        self.status = QLabel(f"Looking up ROMs for {model}...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.others = QCheckBox(tr("Also show other variants of this phone (other regions)"))
        self.others.toggled.connect(self.load)
        layout.addWidget(self.others)

        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)

        credit = QLabel(f'<a style="color:#e6e8ee" href="{catalog.SITE}">{catalog.CREDIT}</a>. '
                        "Packages download from OPPO / OnePlus servers.")
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color:#9aa3b5")
        layout.addWidget(credit)

        row = QHBoxLayout()
        self.download_btn = QPushButton(tr("Download and check"))
        self.download_btn.clicked.connect(self.download)
        self.cancel_btn = QPushButton(tr("Close"))
        self.cancel_btn.clicked.connect(self._close_or_cancel)
        row.addStretch(1)
        row.addWidget(self.download_btn)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)
        self.busy = False
        self._update_buttons()
        self.load()

    # --- list ---------------------------------------------------------------

    def load(self) -> None:
        self.busy = True
        self._update_buttons()
        if self.others.isChecked() and self.releases:
            device = self.releases[0].device
            self.status.setText(f"Looking up all ROMs for {device}...")
            self.run_job(lambda: catalog.fetch(device=device), self._loaded, self._failed)
        else:
            self.run_job(lambda: catalog.for_phone(self.model), self._loaded, self._failed)

    def _loaded(self, releases: list[catalog.Release]) -> None:
        self.busy = False
        own = [r for r in releases if r.model.upper() == self.model.upper()]
        other = [r for r in releases if r.model.upper() != self.model.upper()]
        # Newest first, phone's own model before other variants.
        key = lambda r: (r.build_timestamp, r.version)
        self.releases = sorted(own, key=key, reverse=True) + sorted(other, key=key, reverse=True)
        self.table.setRowCount(len(self.releases))
        for i, r in enumerate(self.releases):
            note = "latest" if r.is_latest else ""
            if r.model.upper() != self.model.upper():
                note = "other variant" + (", latest" if r.is_latest else "")
            cells = (r.version, r.region, r.model, r.security_patch or "-",
                     (r.build_timestamp or "")[:10], r.size_text, note)
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.table.setItem(i, j, item)
        if self.releases:
            self.status.setText(f"{len(own)} ROM(s) for your phone ({self.model})"
                                + (f", {len(other)} for other variants." if other else ".")
                                + " Pick one; it is checked against your phone before anything is flashed.")
            self.table.selectRow(0)
        else:
            self.status.setText(f"The catalog has no ROMs for {self.model} yet.")
        self._update_buttons()

    def _failed(self, exc: BaseException) -> None:
        self.busy = False
        self.progress.hide()
        self.status.setText(str(exc))
        self._update_buttons()

    def selected(self) -> catalog.Release | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.releases[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        self.download_btn.setEnabled(not self.busy and self.selected() is not None)
        self.others.setEnabled(not self.busy and bool(self.releases))
        self.cancel_btn.setText("Cancel" if self.busy and self.progress.isVisible() else "Close")

    # --- download -----------------------------------------------------------

    def download(self) -> None:
        rel = self.selected()
        if rel is None:
            return
        if rel.model.upper() != self.model.upper():
            answer = QMessageBox.question(
                self, "Different variant",
                f"This ROM is for {rel.device} {rel.region} ({rel.model}); your phone is {self.model}.\n\n"
                "Flashing another variant changes the region and needs a data wipe. "
                "Download it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        folder = QFileDialog.getExistingDirectory(self, "Save the ROM in", str(default_download_dir()))
        if not folder:
            return
        self.cancel.clear()
        self.busy = True
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText(f"Downloading {rel.version} ({rel.size_text}) from the official server...")
        self._update_buttons()
        self.run_job(
            lambda emit=None: catalog.download(rel, Path(folder), progress=lambda d, t: emit((d, t)),
                                          cancel=self.cancel.is_set),
            self._downloaded, self._failed, event=self._progress,
        )

    def _progress(self, ev) -> None:
        done, total = ev
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done * 1000 / total))
        self.progress.setFormat(f"{done / 1e6:,.0f} MB" + (f" of {total / 1e6:,.0f} MB" if total else ""))

    def _downloaded(self, path: Path) -> None:
        self.busy = False
        self.downloaded = path
        self.accept()

    def _close_or_cancel(self) -> None:
        if self.busy and self.progress.isVisible():
            self.cancel.set()
            self.status.setText("Cancelling...")
        else:
            self.reject()


def published_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else ""
