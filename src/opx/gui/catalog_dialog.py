"""Pick and download an official ROM from Daniel Springer's OTA catalog."""

from __future__ import annotations

import threading
import time
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

    def __init__(self, parent, run_job, model: str, android: str = "", build: str = ""):
        super().__init__(parent)
        self.android, self.build = android, build
        self.folder: Path | None = None  # where the last download went (resume goes there too)
        self.rate: list[tuple[float, int]] = []
        self.setWindowTitle(tr("Official ROMs"))
        self.resize(820, 480)
        self.run_job = run_job
        self.model = model
        self.releases: list[catalog.Release] = []
        self.downloaded: Path | None = None
        self.converted: dict[str, Path] = {}  # {"folder": ..., "zip": ...} after Download and convert
        self.convert_after: tuple[bool, bool] | None = None  # (image folder, recovery zip)
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
                        "Packages download straight from OPPO / OnePlus servers, the way the phone's "
                        "updater gets them; a stopped download resumes.")
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color:#9aa3b5")
        layout.addWidget(credit)

        conv = QHBoxLayout()
        conv.addWidget(QLabel(tr("Convert for:")))
        self.for_opx = QCheckBox(tr("OPxFlasher (image folder)"))
        self.for_opx.setChecked(True)
        self.for_recovery = QCheckBox(tr("OrangeFox / AERA (recovery zip)"))
        for box in (self.for_opx, self.for_recovery):
            box.toggled.connect(self._update_buttons)
            conv.addWidget(box)
        conv.addStretch(1)
        layout.addLayout(conv)

        row = QHBoxLayout()
        self.download_btn = QPushButton(tr("Download only"))
        self.download_btn.setToolTip(tr("Download the full OTA zip (MD5 checked)"))
        self.download_btn.clicked.connect(self.download)
        self.convert_btn = QPushButton(tr("Download and convert"))
        self.convert_btn.setObjectName("primary")
        self.convert_btn.setToolTip(tr("Download, then make the full ROM you ticked above"))
        self.convert_btn.clicked.connect(self.download_and_convert)
        self.cancel_btn = QPushButton(tr("Close"))
        self.cancel_btn.clicked.connect(self._close_or_cancel)
        row.addStretch(1)
        row.addWidget(self.download_btn)
        row.addWidget(self.convert_btn)
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
                                + " Pick one; it is checked against your phone before anything is flashed."
                                + ("" if catalog.last_source == catalog.API else
                                   f" (The OTA catalog didn't answer; list from "
                                   f"{catalog.describe_source(catalog.last_source)}.)"))
            self.table.selectRow(0)
        else:
            self.status.setText(f"The catalog has no ROMs for {self.model} yet.")
        self._update_buttons()

    def _failed(self, exc: BaseException) -> None:
        self.busy = False
        paused = isinstance(exc, catalog.DownloadPaused)
        if not paused:
            self.progress.hide()
        self.status.setText(str(exc))
        self.log(("Download paused: " if paused else "Official ROMs: ") + str(exc))
        if paused:
            (self.convert_btn if self.convert_after else self.download_btn).setText(tr("Resume download"))
        else:
            self._reset_button_text()
        self._update_buttons()

    def _reset_button_text(self) -> None:
        self.download_btn.setText(tr("Download only"))
        self.convert_btn.setText(tr("Download and convert"))

    def log(self, text: str) -> None:
        """Also into the main window's log, so an exported log shows what happened."""
        say = getattr(self.parent(), "say", None)
        if say:
            say(text)

    def selected(self) -> catalog.Release | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.releases[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        self.download_btn.setEnabled(not self.busy and self.selected() is not None)
        self.convert_btn.setEnabled(not self.busy and self.selected() is not None
                                    and (self.for_opx.isChecked() or self.for_recovery.isChecked()))
        self.for_opx.setEnabled(not self.busy)
        self.for_recovery.setEnabled(not self.busy)
        self.others.setEnabled(not self.busy and bool(self.releases))
        self.cancel_btn.setText("Cancel" if self.busy and self.progress.isVisible() else "Close")

    # --- download -----------------------------------------------------------

    def download_and_convert(self) -> None:
        self.convert_after = (self.for_opx.isChecked(), self.for_recovery.isChecked())
        self._start_download()

    def download(self) -> None:
        self.convert_after = None
        self._start_download()

    def _start_download(self) -> None:
        self.downloaded = None
        self.converted = {}
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
        resuming = self.folder is not None and (self.folder / (rel.filename + ".part")).exists()
        folder = str(self.folder) if resuming else QFileDialog.getExistingDirectory(
            self, "Save the ROM in", str(default_download_dir()))
        if not folder:
            return
        self.folder = Path(folder)
        self.cancel.clear()
        self.busy = True
        self.rate = []
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText(f"Downloading {rel.version} ({rel.size_text}) from the official server...")
        self.log(f"Official ROMs: downloading {rel.model} {rel.version} to {folder}")
        self._update_buttons()
        android, build = self.android, self.build
        self.run_job(
            lambda emit=None: catalog.download(
                rel, Path(folder), progress=lambda d, t: emit(("progress", d, t)),
                stage=lambda s: emit(("stage", s)), cancel=self.cancel.is_set,
                android=android if rel.model.upper() == self.model.upper() else "",
                build=build if rel.model.upper() == self.model.upper() else ""),
            self._downloaded, self._failed, event=self._event,
        )

    def _event(self, ev) -> None:
        if ev[0] == "stage":
            self.status.setText(ev[1])
            self.log(ev[1])
            if ev[1].startswith("Checking"):
                self.rate = []  # the check re-counts from zero
            return
        _, done, total = ev
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done * 1000 / total))
        now = time.monotonic()
        self.rate = [(t, d) for t, d in self.rate if now - t < 8] + [(now, done)]
        speed = ""
        if len(self.rate) > 1 and now - self.rate[0][0] > 1:
            bps = (done - self.rate[0][1]) / (now - self.rate[0][0])
            if bps > 0:
                speed = f"  ·  {bps / 1e6:.1f} MB/s"
                if total and total > done:
                    left = int((total - done) / bps)
                    speed += f"  ·  {left // 60} min {left % 60:02d} s left"
        self.progress.setFormat(f"{done / 1e6:,.0f} MB" + (f" of {total / 1e6:,.0f} MB" if total else "") + speed)

    def _downloaded(self, path: Path) -> None:
        self.downloaded = path
        self._reset_button_text()
        if not self.convert_after:
            self.busy = False
            self.accept()
            return
        make_folder, make_zip = self.convert_after
        self.cancel.clear()
        self.rate = []
        self.progress.setRange(0, 0)
        self.status.setText(tr("Converting to a full ROM..."))
        self.log(f"Convert: {path.name} -> {path.parent}"
                 + (" (image folder" + (" + recovery zip)" if make_zip else ")") if make_folder else " (recovery zip)"))
        cancel = self.cancel.is_set
        self.run_job(
            lambda emit=None: convert_download(path, make_folder, make_zip,
                                               progress=lambda w, n, t: emit(("progress", n, t)),
                                               stage=lambda s: emit(("stage", s)), cancel=cancel),
            self._converted, self._convert_failed, event=self._event)

    def _converted(self, made: dict) -> None:
        self.busy = False
        self.converted = made
        for kind, label in (("folder", "Image folder"), ("zip", "Recovery zip")):
            if kind in made:
                self.log(f"Convert: {label}: {made[kind]}")
        self.accept()

    def _convert_failed(self, exc: BaseException) -> None:
        # the download itself is kept; only the conversion failed or was stopped
        self.busy = False
        self.progress.hide()
        self.status.setText(tr("Downloaded, but converting failed:") + f" {exc}")
        self.log(f"Convert failed: {exc}")
        self._update_buttons()

    @property
    def result_path(self) -> Path | None:
        """What to check next: the converted image folder, else the zip made, else the download."""
        return self.converted.get("folder") or self.converted.get("zip") or self.downloaded

    def _close_or_cancel(self) -> None:
        if self.busy and self.progress.isVisible():
            self.cancel.set()
            converting = self.convert_after and self.downloaded is not None
            self.status.setText(tr("Stopping the conversion... (the download is kept)") if converting
                                else "Pausing... (the download can be resumed)")
        else:
            self.reject()


def convert_download(path: Path, make_folder: bool, make_zip: bool, progress=None, stage=None, cancel=None) -> dict:
    """Convert a downloaded full OTA next to it."""
    from opx import convert as cv
    from opx.rom.ota import open_rom

    return cv.convert(open_rom(path), path.parent, make_zip=make_zip, make_folder=make_folder,
                      progress=progress, stage=stage, cancel=cancel)


def published_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else ""
