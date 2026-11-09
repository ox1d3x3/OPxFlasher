"""The confirmation dialog shown before anything is written to the phone."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
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
        self.resize(760, 720)
        layout = QVBoxLayout(self)

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

        # the one overridable block
        self.arb_edit: QLineEdit | None = None
        if fl.overridable_blocks(report):
            a_box = QGroupBox("Qualcomm anti-rollback: not validated")
            a_box.setStyleSheet("QGroupBox { border-color: #8a2b26; }")
            a_layout = QVBoxLayout(a_box)
            detail = "\n".join(c.detail for c in fl.overridable_blocks(report))
            warn = QLabel(
                "OPxFlasher can't yet confirm the Qualcomm anti-rollback version for this phone.\n"
                "Flashing firmware with a LOWER anti-rollback version than the phone hard-bricks it permanently.\n\n"
                f"{detail}\n\nOnly continue if you have confirmed these numbers yourself. Type ARB CHECKED:"
            )
            warn.setWordWrap(True)
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
        layout.addWidget(self.preview, 1)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel("Type FLASH to enable the button:"))
        self.confirm_edit = QLineEdit()
        confirm_row.addWidget(self.confirm_edit, 1)
        layout.addLayout(confirm_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.start_btn = QPushButton("Start flashing")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.start_btn)
        layout.addLayout(btns)

        for cb in [*self.optional_boxes.values(), self.wipe_box, self.reboot_box, self.fastbootd_box]:
            cb.toggled.connect(self.refresh)
        for edit in (self.backup_edit, self.confirm_edit, self.arb_edit):
            if edit is not None:
                edit.textChanged.connect(self.refresh)
        self.refresh()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose your backup of this phone", self.backup_edit.text())
        if folder:
            self.backup_edit.setText(folder)

    def options(self) -> fl.FlashOptions:
        arb_ok = self.arb_edit is not None and self.arb_edit.text().strip() == "ARB CHECKED"
        return fl.FlashOptions(
            include_optional=[p for p, cb in self.optional_boxes.items() if cb.isChecked()],
            wipe_data=self.wipe_box.isChecked(),
            reboot_after=self.reboot_box.isChecked(),
            allow_fastbootd=self.fastbootd_box.isChecked(),
            overrides=[fl.OVERRIDE_QCOM_UNVALIDATED] if arb_ok else [],
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
