"""Flash image tab: write one image (boot, init_boot, recovery, vbmeta, dtbo, ...)."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opx.i18n import tr
from opx import flash as fl
from opx import plan as pn
from opx import single as sg
from opx.tools import Tool

PARTITIONS = ("boot", "init_boot", "recovery", "vendor_boot", "dtbo", "vbmeta", "vbmeta_system", "vbmeta_vendor")
SLOT_TEXT = {"current": "Current slot", "other": "Other slot", "both": "Both slots"}


class ImageTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.plan: fl.FlashPlan | None = None
        layout = QVBoxLayout(self)

        intro = QLabel("Flash a single image, for example a recovery, a patched boot or a vbmeta. "
                       "Partitions inside 'super' (system, vendor, ...) need a full ROM flash instead.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#9aa3b5")
        layout.addWidget(intro)

        box = QGroupBox(tr("Image"))
        form = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Partition:")))
        self.part_combo = QComboBox()
        self.part_combo.setEditable(True)
        self.part_combo.addItems(PARTITIONS)
        self.part_combo.setMinimumWidth(160)
        row.addWidget(self.part_combo)
        row.addWidget(QLabel(tr("Slot:")))
        self.slot_combo = QComboBox()
        for key, text in SLOT_TEXT.items():
            self.slot_combo.addItem(text, key)
        row.addWidget(self.slot_combo)
        row.addStretch(1)
        form.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("File:")))
        self.image_edit = QLineEdit()
        self.image_edit.setPlaceholderText("The .img file to flash")
        row.addWidget(self.image_edit, 1)
        pick = QPushButton(tr("Browse..."))
        pick.clicked.connect(self._pick_image)
        row.addWidget(pick)
        form.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Backup:")))
        self.backup_edit = QLineEdit()
        self.backup_edit.setPlaceholderText("A Critical or Full backup of this phone (required)")
        row.addWidget(self.backup_edit, 1)
        pick_b = QPushButton(tr("Browse..."))
        pick_b.clicked.connect(self._pick_backup)
        row.addWidget(pick_b)
        form.addLayout(row)

        self.verity = QCheckBox(tr("Disable verity and verification (vbmeta only; some custom kernels need it)"))
        form.addWidget(self.verity)
        self.reboot_after = QCheckBox(tr("Reboot to Android afterwards"))
        self.reboot_after.setChecked(True)
        form.addWidget(self.reboot_after)
        layout.addWidget(box)

        row = QHBoxLayout()
        self.check_btn = QPushButton(tr("Check"))
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Type FLASH:")))
        self.confirm = QLineEdit()
        self.confirm.setMaximumWidth(140)
        self.confirm.textChanged.connect(lambda _t: self.refresh_buttons())
        row.addWidget(self.confirm)
        self.flash_btn = QPushButton(tr("Flash"))
        self.flash_btn.setObjectName("danger")
        self.flash_btn.clicked.connect(self.flash)
        row.addWidget(self.flash_btn)
        self.stop_btn = QPushButton(tr("Stop after this step"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(win._stop_flash)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.label = QLabel("")
        self.label.hide()
        layout.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.hide()
        layout.addWidget(self.bar)
        layout.addStretch(1)

        for w in (self.image_edit, self.backup_edit):
            w.textChanged.connect(self._invalidate)
        self.part_combo.currentTextChanged.connect(self._invalidate)
        self.slot_combo.currentIndexChanged.connect(self._invalidate)
        self.verity.toggled.connect(self._invalidate)
        self.reboot_after.toggled.connect(self._invalidate)
        self.part_combo.currentTextChanged.connect(self._sync_verity)
        self._sync_verity(self.part_combo.currentText())

    # --- inputs ------------------------------------------------------------

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.img *.bin);;All files (*)")
        if path:
            self.image_edit.setText(path)

    def _pick_backup(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a backup of this phone")
        if folder:
            self.backup_edit.setText(folder)

    def _sync_verity(self, part: str) -> None:
        is_vb = part.strip().startswith("vbmeta")
        self.verity.setEnabled(is_vb)
        if not is_vb:
            self.verity.setChecked(False)

    def _invalidate(self, *_a) -> None:
        self.plan = None
        self.summary.setText("")
        self.confirm.clear()
        self.refresh_buttons()

    def _device(self) -> pn.DeviceState | None:
        if self.win.session is not None:
            return pn.device_from_session(self.win.session)
        backup = Path(self.backup_edit.text().strip() or ".")
        d = self.win.selected_device()
        if d is not None and d.transport == "fastboot" and (backup / "manifest.json").exists():
            return pn.device_from_backup(backup)  # phone in fastboot: the backup tells us which phone
        return None

    # --- check and flash --------------------------------------------------------

    def check(self) -> None:
        device = self._device()
        if device is None:
            self.summary.setText("Connect the phone in Android, or put it in fastboot and choose its backup.")
            return
        backup = self.backup_edit.text().strip()
        image = self.image_edit.text().strip()
        if not image:
            self.summary.setText("Choose the image file.")
            return
        try:
            self.plan, warnings = sg.prepare_image_flash(
                device, Path(backup) if backup else None, self.part_combo.currentText().strip(), Path(image),
                self.slot_combo.currentData(), self.verity.isChecked(), self.reboot_after.isChecked())
        except ValueError as e:
            self.plan = None
            self.summary.setText(str(e))
            return
        lines = [f"{i}. {s.label}" for i, s in enumerate(self.plan.steps, 1)]
        lines += [f"! {w}" for w in warnings]
        if self.plan.ok:
            lines.append("Ready. Type FLASH to enable the button.")
            self.summary.setStyleSheet("")
        else:
            lines.append("Stopped by:")
            lines += [f"- {p}" for p in self.plan.problems]
            self.summary.setStyleSheet("color:#f0a0a0")
        self.summary.setText("\n".join(lines))
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        busy = self.win.busy
        self.check_btn.setEnabled(not busy)
        ready = self.plan is not None and self.plan.ok and self.confirm.text().strip() == "FLASH"
        self.flash_btn.setEnabled(not busy and ready)

    def flash(self) -> None:
        win = self.win
        if not (self.plan and self.plan.ok):
            return
        work = Path(self.backup_edit.text().strip()).parent / "opx-flash-image"
        work.mkdir(parents=True, exist_ok=True)
        device = self._device()
        if device is None:
            QMessageBox.warning(self, "Flash image", "The phone is no longer connected. Check again.")
            return
        win.flash_cancel = threading.Event()
        journal = work / "journal.json"
        runner = fl.Runner(self.plan, None, device.profile, Tool("adb"), Tool("fastboot"), journal,
                           work / "images", cancel=win.flash_cancel)
        self.confirm.clear()
        win._launch(runner, journal, self.label, self.bar, self.stop_btn, "Flash image")
