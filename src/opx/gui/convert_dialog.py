"""ROM check > Convert to full ROM...: detect what a ROM is, then make a recovery
zip (OrangeFox / TWRP / AERA) and/or an image folder from it."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from opx import convert as cv
from opx.i18n import tr
from opx.rom import detect


class ConvertDialog(QDialog):
    """`run_job(fn, done, failed, event=None)` as in CatalogDialog. After a
    conversion, `made` holds {"zip": path, "folder": path} for what was written."""

    def __init__(self, parent, run_job, rom_path: str):
        super().__init__(parent)
        self.setWindowTitle(tr("Convert to full ROM"))
        self.resize(760, 480)
        self.run_job = run_job
        self.rom_path = Path(rom_path)
        self.detection: detect.Detection | None = None
        self.made: dict[str, Path] = {}
        self.cancel = threading.Event()
        self.busy = False

        layout = QVBoxLayout(self)
        intro = QLabel(tr("Makes a full ROM that installs from a custom recovery (OrangeFox, TWRP, AERA) "
                          "or from OPxFlasher, keeping your data. The recovery zip checks the phone, the "
                          "variant and that the ROM isn't older than what's installed before it writes anything."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.report.setFont(mono)
        self.report.setPlainText(f"Looking at {self.rom_path.name}...")
        layout.addWidget(self.report, 1)

        self.want_folder = QCheckBox(tr("OPxFlasher image folder (check and flash it from OPxFlasher)"))
        self.want_folder.setChecked(True)
        self.want_zip = QCheckBox(tr("Also a recovery zip for OrangeFox / TWRP / AERA"))
        for box in (self.want_folder, self.want_zip):
            box.toggled.connect(self._update_buttons)
            layout.addWidget(box)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel(tr("Save in:")))
        self.out_edit = QLineEdit(str(self.rom_path.parent))
        out_row.addWidget(self.out_edit, 1)
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        out_row.addWidget(browse)
        layout.addLayout(out_row)

        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.open_btn = QPushButton(tr("Open folder"))
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_edit.text())))
        self.open_btn.hide()
        self.use_btn = QPushButton(tr("Use in ROM check"))
        self.use_btn.setToolTip(tr("Check the converted ROM against the phone (and flash it from OPxFlasher)"))
        self.use_btn.clicked.connect(self.accept)
        self.use_btn.hide()
        self.convert_btn = QPushButton(tr("Convert"))
        self.convert_btn.setObjectName("primary")
        self.convert_btn.clicked.connect(self.start)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self._close_or_cancel)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        row.addWidget(self.use_btn)
        row.addWidget(self.convert_btn)
        row.addWidget(self.close_btn)
        layout.addLayout(row)

        self.busy = True
        self._update_buttons()
        self.run_job(lambda: detect.detect(self.rom_path), self._detected, self._failed)

    # --- detection ----------------------------------------------------------------

    def _detected(self, d: detect.Detection) -> None:
        self.busy = False
        self.detection = d
        self.report.setPlainText("\n".join(d.lines()))
        ok, why = d.convertible
        if ok and self.want_zip.isChecked():
            rom_ok = bool(d.device and d.product) and bool(d.built or d.security_patch)
            if not rom_ok:
                self.want_zip.setChecked(False)
                self.want_folder.setChecked(True)
                self.status.setText(tr("This ROM doesn't say which phone it's for or how new it is, so a "
                                       "recovery couldn't check it: only an image folder can be made."))
        if not ok:
            self.status.setText(why)
        self.log(f"Convert: {d.label or 'not a ROM'}" + (f" for {d.product}" if d.product else ""))
        self._update_buttons()

    def _failed(self, exc: BaseException) -> None:
        self.busy = False
        self.progress.hide()
        self.status.setText(str(exc))
        self.log(f"Convert: {exc}")
        self._update_buttons()

    def log(self, text: str) -> None:
        say = getattr(self.parent(), "say", None)
        if say:
            say(text)

    # --- conversion -------------------------------------------------------------------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Save the full ROM in"), self.out_edit.text())
        if folder:
            self.out_edit.setText(folder)

    def start(self) -> None:
        d = self.detection
        if d is None or not d.convertible[0]:
            return
        out = Path(self.out_edit.text().strip())
        make_zip, make_folder = self.want_zip.isChecked(), self.want_folder.isChecked()
        self.cancel.clear()
        self.busy = True
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText(tr("Converting..."))
        self.log(f"Convert: {d.rom.path.name} -> {out}")
        self._update_buttons()
        rom = d.rom
        self.run_job(
            lambda emit=None: cv.convert(rom, out, make_zip=make_zip, make_folder=make_folder,
                                         progress=lambda w, n, t: emit(("progress", w, n, t)),
                                         stage=lambda s: emit(("stage", s)), cancel=self.cancel.is_set),
            self._done, self._failed, event=self._event,
        )

    def _event(self, ev) -> None:
        if ev[0] == "stage":
            self.status.setText(ev[1])
            self.log(f"  {ev[1]}")
            return
        _, what, done, total = ev
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done * 1000 / total))
        self.progress.setFormat(f"{what}  ·  {done / 1e9:.2f} of {total / 1e9:.2f} GB")

    def _done(self, made: dict) -> None:
        self.busy = False
        self.made = made
        self.progress.setValue(1000)
        lines = []
        if "zip" in made:
            lines.append(f"Recovery zip: {made['zip'].name} - copy it to the phone and flash it in OrangeFox, "
                         "TWRP or AERA.")
        if "folder" in made:
            lines.append(f"Image folder: {made['folder'].name} - ROM check and flash it with OPxFlasher.")
        self.status.setText("\n".join(lines))
        for ln in lines:
            self.log(f"Convert: {ln}")
        self.open_btn.show()
        self.use_btn.show()
        self._update_buttons()

    @property
    def result_path(self) -> Path | None:
        return self.made.get("folder") or self.made.get("zip")

    def _close_or_cancel(self) -> None:
        if self.busy and self.progress.isVisible():
            self.cancel.set()
            self.status.setText(tr("Cancelling... (nothing is kept)"))
        else:
            self.reject()

    def _update_buttons(self) -> None:
        ok = self.detection is not None and self.detection.convertible[0]
        chosen = self.want_zip.isChecked() or self.want_folder.isChecked()
        self.convert_btn.setEnabled(ok and chosen and not self.busy and not self.made)
        for box in (self.want_zip, self.want_folder):
            box.setEnabled(ok and not self.busy and not self.made)
        self.close_btn.setText(tr("Cancel") if self.busy and self.progress.isVisible() else tr("Close"))
