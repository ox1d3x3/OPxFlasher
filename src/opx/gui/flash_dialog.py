"""The confirmation dialog shown before anything is written to the phone."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from opx import flash as fl
from opx import plan as pn
from opx.rom.ota import RomInfo


class FlashDialog(QDialog):
    def __init__(self, parent, rom: RomInfo, device: pn.DeviceState, report: pn.Report, backup_hint: str = ""):
        super().__init__(parent)
        self.rom, self.device, self.report = rom, device, report
        self.plan: fl.FlashPlan | None = None
        self.setWindowTitle("Flash firmware")
        self.resize(820, 860)
        outer = QVBoxLayout(self)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        head = QLabel(f"<b>Flash {rom.path.name}</b><br>to {device.label}")
        head.setWordWrap(True)
        layout.addWidget(head)

        # backup (required)
        b_box = QGroupBox("Your backup of this phone (required)")
        b_layout = QVBoxLayout(b_box)
        row = QHBoxLayout()
        self.backup_edit = QLineEdit(backup_hint)
        self.backup_edit.setPlaceholderText("A Critical or Full backup made by OPxFlasher")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.backup_edit, 1)
        row.addWidget(browse)
        b_layout.addLayout(row)
        layout.addWidget(b_box)

        # options
        o_box = QGroupBox("Options")
        o_layout = QVBoxLayout(o_box)
        self.optional_boxes: dict[str, QCheckBox] = {}
        for item in report.items:
            if item.action == "conditional":
                cb = QCheckBox(f"Also flash {item.partition} from the ROM (region / signal file - usually leave off)")
                self.optional_boxes[item.partition] = cb
                o_layout.addWidget(cb)
        self.wipe_box = QCheckBox("Wipe data (factory reset) - needed when changing region or OS. ERASES EVERYTHING.")
        self.reboot_box = QCheckBox("Reboot to Android when finished")
        self.reboot_box.setChecked(True)
        has_logical = any(i.action == "super" for i in report.items)
        fastbootd_blocked = "fastbootd" in device.profile.blocked_actions(device.props)
        self.fastbootd_box = QCheckBox("Use fastbootd for logical partitions (system, my_region ...)")
        self.fastbootd_box.setChecked(has_logical and not fastbootd_blocked)
        self.fastbootd_box.setEnabled(has_logical and not fastbootd_blocked)
        for w in (self.wipe_box, self.reboot_box, self.fastbootd_box):
            o_layout.addWidget(w)
        layout.addWidget(o_box)

        # region conversion
        self.keep_region_box: QCheckBox | None = None
        if fl.region_change(report):
            rom_product = report.rom.get("product") or "?"
            dev_product = device.facts.get("product") or "?"
            r_box = QGroupBox(f"Region conversion: {dev_product} -> {rom_product}")
            r_layout = QVBoxLayout(r_box)
            self.keep_region_box = QCheckBox(
                "Keep mobile signal: restore this phone's own region files from the backup (recommended)"
            )
            self.keep_region_box.setChecked(True)
            note = QLabel(
                "Your oplusstanvbk and my_region are written back after the new firmware, so the modem keeps "
                "the setup that works with your SIM. Changing region needs a factory reset, so Wipe data is "
                "switched on and locked."
            )
            note.setStyleSheet("color:#9aa3b5")
            note.setWordWrap(True)
            r_layout.addWidget(self.keep_region_box)
            r_layout.addWidget(note)
            layout.addWidget(r_box)
            self.wipe_box.setChecked(True)
            self.wipe_box.setEnabled(False)
            self.keep_region_box.toggled.connect(self._sync_optional)
            self._sync_optional()

        # different variant of the same phone: the user must say yes (default: no)
        self.variant_box: QCheckBox | None = None
        variant = fl.overridable_blocks(report, fl.OVERRIDE_VARIANT)
        if variant:
            v_box = QGroupBox("Different variant")
            v_box.setStyleSheet("QGroupBox { border-color: #8a5a26; }")
            v_layout = QVBoxLayout(v_box)
            v_text = QLabel(variant[0].detail.replace(" Confirm the variant change to continue.", ""))
            v_text.setWordWrap(True)
            v_text.setStyleSheet("color:#ffd29a")
            self.variant_box = QCheckBox("Yes, I want to flash this other variant")
            v_layout.addWidget(v_text)
            v_layout.addWidget(self.variant_box)
            layout.addWidget(v_box)

        # unvalidated Qualcomm anti-rollback reader
        self.arb_edit: QLineEdit | None = None
        if fl.overridable_blocks(report, fl.OVERRIDE_QCOM_UNVALIDATED):
            a_box = QGroupBox("Qualcomm anti-rollback: not validated")
            a_box.setStyleSheet("QGroupBox { border-color: #8a2b26; }")
            a_layout = QVBoxLayout(a_box)
            detail = "\n".join(c.detail for c in fl.overridable_blocks(report, fl.OVERRIDE_QCOM_UNVALIDATED))
            warn = QLabel(
                "OPxFlasher can't yet confirm the Qualcomm anti-rollback version for this phone.\n"
                "Flashing firmware with a LOWER anti-rollback version than the phone hard-bricks it permanently.\n\n"
                f"{detail}\n\nOnly continue if you have confirmed these numbers yourself. Type ARB CHECKED:"
            )
            warn.setWordWrap(True)
            warn.setMinimumHeight(warn.sizeHint().height() + 40)
            warn.setStyleSheet("color:#ffb3ad")
            self.arb_edit = QLineEdit()
            self.arb_edit.setPlaceholderText("ARB CHECKED")
            a_layout.addWidget(warn)
            a_layout.addWidget(self.arb_edit)
            layout.addWidget(a_box)

        # plan preview
        layout.addWidget(QLabel("What will happen:"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(220)
        layout.addWidget(self.preview, 1)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel("Type FLASH to enable the button:"))
        self.confirm_edit = QLineEdit()
        confirm_row.addWidget(self.confirm_edit, 1)
        outer.addLayout(confirm_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.start_btn = QPushButton("Start flashing")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.start_btn)
        outer.addLayout(btns)

        extra = [b for b in (self.keep_region_box, self.variant_box) if b is not None]
        for cb in [*self.optional_boxes.values(), self.wipe_box, self.reboot_box, self.fastbootd_box, *extra]:
            cb.toggled.connect(self.refresh)
        for edit in (self.backup_edit, self.confirm_edit, self.arb_edit):
            if edit is not None:
                edit.textChanged.connect(self.refresh)
        self.refresh()

    def _sync_optional(self) -> None:
        """The ROM's own region files can't be chosen while restoring the phone's."""
        keep = bool(self.keep_region_box and self.keep_region_box.isChecked())
        for cb in self.optional_boxes.values():
            if keep:
                cb.setChecked(False)
            cb.setEnabled(not keep)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose your backup of this phone", self.backup_edit.text())
        if folder:
            self.backup_edit.setText(folder)

    def options(self) -> fl.FlashOptions:
        arb_ok = self.arb_edit is not None and self.arb_edit.text().strip() == "ARB CHECKED"
        backup = self.backup_edit.text().strip()
        restore = []
        if self.keep_region_box is not None and self.keep_region_box.isChecked():
            restore = [n for n in self.device.profile.conditional if fl.backup_images(backup, n)]
        return fl.FlashOptions(
            restore_from_backup=restore,
            include_optional=[p for p, cb in self.optional_boxes.items() if cb.isChecked()],
            wipe_data=self.wipe_box.isChecked(),
            reboot_after=self.reboot_box.isChecked(),
            allow_fastbootd=self.fastbootd_box.isChecked(),
            overrides=([fl.OVERRIDE_QCOM_UNVALIDATED] if arb_ok else [])
            + ([fl.OVERRIDE_VARIANT] if self.variant_box is not None and self.variant_box.isChecked() else []),
        )

    def refresh(self) -> None:
        backup = self.backup_edit.text().strip()
        self.plan = fl.prepare(self.rom, self.report, self.device, self.options(), Path(backup) if backup else None)
        lines = [f"{i:>2}. {s.label}" for i, s in enumerate(self.plan.steps, 1)]
        if self.plan.problems:
            lines += ["", "BLOCKED - fix these first:"] + [f"  - {p}" for p in self.plan.problems]
        else:
            lines += ["", "All safety gates passed."]
            if self.wipe_box.isChecked():
                lines.append("All data on the phone will be erased.")
        self.preview.setPlainText("\n".join(lines))
        typed = self.confirm_edit.text().strip() == "FLASH"
        self.start_btn.setEnabled(self.plan.ok and typed)
