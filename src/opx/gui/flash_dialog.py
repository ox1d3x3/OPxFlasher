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
from opx.i18n import tr
from opx.rom.ota import RomInfo


class FlashDialog(QDialog):
    def __init__(self, parent, rom: RomInfo, device: pn.DeviceState, report: pn.Report, backup_hint: str = ""):
        super().__init__(parent)
        self.rom, self.device, self.report = rom, device, report
        self.plan: fl.FlashPlan | None = None
        self.setWindowTitle(tr("Flash firmware"))
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

        # backup (recommended, not required)
        b_box = QGroupBox(tr("Your backup of this phone (recommended)"))
        b_layout = QVBoxLayout(b_box)
        row = QHBoxLayout()
        self.backup_edit = QLineEdit(backup_hint)
        self.backup_edit.setPlaceholderText(tr("A Critical or Full backup made by OPxFlasher"))
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        row.addWidget(self.backup_edit, 1)
        row.addWidget(browse)
        b_layout.addLayout(row)
        self.no_backup = QCheckBox(tr("Flash without a backup: I agree with the risk"))
        self.no_backup.setToolTip(tr("A backup lets you go back if something goes wrong. Without one, the ROM can "
                                     "still be flashed again. Every other safety check stays on."))
        b_layout.addWidget(self.no_backup)
        b_hint = QLabel(tr("Recommended: Backup tab > Critical (needs root). Without root, tick the box above."))
        b_hint.setStyleSheet("color:#9aa3b5")
        b_hint.setWordWrap(True)
        b_layout.addWidget(b_hint)
        layout.addWidget(b_box)

        # options
        o_box = QGroupBox(tr("Options"))
        o_layout = QVBoxLayout(o_box)
        self.optional_boxes: dict[str, QCheckBox] = {}
        for item in report.items:
            if item.action == "conditional":
                cb = QCheckBox(f"Also flash {item.partition} from the ROM (region / signal file - usually leave off)")
                self.optional_boxes[item.partition] = cb
                o_layout.addWidget(cb)
        self.wipe_box = QCheckBox(tr("Wipe data (factory reset) - needed when changing region or OS. ERASES EVERYTHING."))
        self.reboot_box = QCheckBox(tr("Reboot to Android when finished"))
        self.reboot_box.setChecked(True)
        self.keep_recovery_box = QCheckBox(tr("Keep my custom recovery (don't write the ROM's recovery)"))
        self.keep_recovery_box.setToolTip(tr("AERA, OrangeFox or TWRP stays installed. Not possible when part of "
                                             "the flash runs in fastbootd (it starts from the recovery partition): "
                                             "then the ROM's recovery is written and you're told so."))
        has_recovery = any(i.partition == "recovery" and i.action == "flash" for i in report.items)
        self.keep_recovery_box.setEnabled(has_recovery)
        has_logical = any(i.action == "super" for i in report.items)
        fastbootd_blocked = "fastbootd" in device.profile.blocked_actions(device.props)
        self.fastbootd_box = QCheckBox(tr("Use fastbootd for logical partitions (system, my_region ...)"))
        self.fastbootd_box.setChecked(has_logical and not fastbootd_blocked)
        self.fastbootd_box.setEnabled(has_logical and not fastbootd_blocked)
        for w in (self.wipe_box, self.reboot_box, self.fastbootd_box, self.keep_recovery_box):
            o_layout.addWidget(w)
        layout.addWidget(o_box)

        # region conversion
        self.keep_region_box: QCheckBox | None = None
        self.net_edit: QLineEdit | None = None
        if fl.region_change(report):
            rom_product = report.rom.get("product") or "?"
            dev_product = device.facts.get("product") or "?"
            r_box = QGroupBox(f"Region conversion: {dev_product} -> {rom_product}")
            r_layout = QVBoxLayout(r_box)
            self.keep_region_box = QCheckBox(
                "Keep mobile signal: restore this phone's own region files from the backup (recommended)"
            )
            self.keep_region_box.setChecked(True)
            kept = fl.keepable(report, device.profile.conditional) or ["none"]
            note = QLabel(
                f"Written back from your backup after the new firmware: {', '.join(kept)}, so the modem keeps "
                "the setup that works with your SIM. Region files this ROM's vbmeta checks at boot always come "
                "from the ROM. Changing region needs a factory reset, so Wipe data is switched on and locked."
            )
            note.setStyleSheet("color:#9aa3b5")
            note.setWordWrap(True)
            self.region_warn = QLabel(tr("Without your own region files, mobile network may not work after this "
                                         "flash. You can still restore them later: Signal / region tab, from your "
                                         "backup."))
            self.region_warn.setStyleSheet("color:#f0c24b")
            self.region_warn.setWordWrap(True)
            self.region_warn.hide()
            r_layout.addWidget(self.keep_region_box)
            r_layout.addLayout(self._net_row())
            r_layout.addWidget(note)
            r_layout.addWidget(self.region_warn)
            layout.addWidget(r_box)
            self.wipe_box.setChecked(True)
            self.wipe_box.setEnabled(False)
            self.keep_region_box.toggled.connect(self._sync_optional)
            self._sync_optional()
        elif fl.keepable(report, device.profile.conditional):
            # same region: the phone keeps its own network files (they aren't written). A phone converted
            # from another region earlier can have its own ones written again from a backup.
            n_box = QGroupBox(tr("Mobile network files"))
            n_layout = QVBoxLayout(n_box)
            names = ", ".join(fl.keepable(report, device.profile.conditional))
            n_text = QLabel(tr("Not written by this flash: the phone keeps its own") + f" {names}.")
            n_text.setWordWrap(True)
            n_text.setStyleSheet("color:#9aa3b5")
            self.keep_region_box = QCheckBox(tr("This phone was converted from another region: also write my own "
                                                "network files from my backup (Network essentials or Critical)"))
            self.region_warn = QLabel(tr("Choose the backup with your network files (the one made before "
                                         "converting)."))
            self.region_warn.setStyleSheet("color:#f0c24b")
            self.region_warn.setWordWrap(True)
            self.region_warn.hide()
            n_layout.addWidget(n_text)
            n_layout.addWidget(self.keep_region_box)
            n_layout.addLayout(self._net_row())
            n_layout.addWidget(self.region_warn)
            layout.addWidget(n_box)
            self.keep_region_box.toggled.connect(self._sync_optional)

        # different variant of the same phone: the user must say yes (default: no)
        self.variant_box: QCheckBox | None = None
        variant = fl.overridable_blocks(report, fl.OVERRIDE_VARIANT)
        if variant:
            v_box = QGroupBox(tr("Different variant"))
            v_box.setStyleSheet("QGroupBox { border-color: #8a5a26; }")
            v_layout = QVBoxLayout(v_box)
            v_text = QLabel(variant[0].detail.replace(" Confirm the variant change to continue.", ""))
            v_text.setWordWrap(True)
            v_text.setStyleSheet("color:#ffd29a")
            self.variant_box = QCheckBox(tr("Yes, I want to flash this other variant"))
            v_layout.addWidget(v_text)
            v_layout.addWidget(self.variant_box)
            layout.addWidget(v_box)

        # full ROM restore with the phone not in Android: user confirms no update since the backup
        self.not_updated_box: QCheckBox | None = None
        unknown_fw = fl.overridable_blocks(report, fl.OVERRIDE_UNKNOWN_FIRMWARE)
        if unknown_fw:
            f_box = QGroupBox(tr("Phone's current firmware is unknown"))
            f_box.setStyleSheet("QGroupBox { border-color: #8a2b26; }")
            f_layout = QVBoxLayout(f_box)
            f_text = QLabel(unknown_fw[0].detail)
            f_text.setWordWrap(True)
            f_text.setStyleSheet("color:#ffb3ad")
            self.not_updated_box = QCheckBox(tr("This phone has NOT installed any update since this backup"))
            f_layout.addWidget(f_text)
            f_layout.addWidget(self.not_updated_box)
            layout.addWidget(f_box)

        # anti-rollback the app can't read or trust: the user's own, deliberate confirmation
        self.arb_accepted = False
        self.arb_btn: QPushButton | None = None
        self.arb_details = [f"{c.name}: {c.detail.split('.')[0]}."
                            for key in fl.RISK_OVERRIDES for c in fl.overridable_blocks(report, key)]
        if self.arb_details:
            a_box = QGroupBox(tr("Anti-rollback: your confirmation"))
            a_box.setStyleSheet("QGroupBox { border-color: #8a2b26; }")
            a_layout = QVBoxLayout(a_box)
            warn = QLabel(tr("OPxFlasher can't check the Qualcomm anti-rollback version here. Flashing older "
                             "firmware than the phone's can brick it permanently. Read the warning and accept the "
                             "risk yourself to continue."))
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#ffb3ad")
            self.arb_btn = QPushButton(tr("Read the warning and accept the risk..."))
            self.arb_btn.setObjectName("danger")
            self.arb_btn.clicked.connect(self._ask_arb)
            self.arb_status = QLabel()
            self.arb_status.setWordWrap(True)
            a_layout.addWidget(warn)
            a_layout.addWidget(self.arb_status)
            a_layout.addWidget(self.arb_btn)
            layout.addWidget(a_box)
            self._show_arb_validation()

        # plan preview
        layout.addWidget(QLabel(tr("What will happen:")))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(220)
        layout.addWidget(self.preview, 1)

        confirm_row = QHBoxLayout()
        self.confirm_label = QLabel(tr("Type FLASH to enable the button:"))
        confirm_row.addWidget(self.confirm_label)
        self.confirm_edit = QLineEdit()
        confirm_row.addWidget(self.confirm_edit, 1)
        outer.addLayout(confirm_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.start_btn = QPushButton(tr("Start flashing"))
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.start_btn)
        outer.addLayout(btns)

        extra = [b for b in (self.keep_region_box, self.variant_box, self.not_updated_box) if b is not None]
        for cb in [*self.optional_boxes.values(), self.wipe_box, self.reboot_box, self.fastbootd_box,
                   self.keep_recovery_box, self.no_backup, *extra]:
            cb.toggled.connect(self.refresh)
        for edit in (self.backup_edit, self.confirm_edit, self.net_edit):
            if edit is not None:
                edit.textChanged.connect(self.refresh)
        self.refresh()

    def _net_row(self) -> QHBoxLayout:
        """Where your own network files come from: their own backup (Network essentials is enough),
        or, left empty, the backup chosen at the top."""
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Network files backup:")))
        self.net_edit = QLineEdit()
        self.net_edit.setPlaceholderText(tr("Network essentials (or Critical) backup of this phone; empty = the "
                                            "backup above"))
        row.addWidget(self.net_edit, 1)
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse_net)
        row.addWidget(browse)
        return row

    def _browse_net(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Your Network essentials backup of this phone"),
                                                  self.net_edit.text() if self.net_edit else "")
        if folder and self.net_edit is not None:
            self.net_edit.setText(folder)

    def _net_folder(self) -> str:
        net = self.net_edit.text().strip() if self.net_edit is not None else ""
        return net or self.backup_edit.text().strip()

    def _sync_optional(self) -> None:
        """The ROM's own region files can't be chosen while restoring the phone's."""
        keep = bool(self.keep_region_box and self.keep_region_box.isChecked())
        if self.keep_region_box is not None:
            region = fl.region_change(self.report)
            # region change: warn when NOT keeping; converted phone: remind to pick the backup when keeping
            self.region_warn.setVisible((not keep) if region else (keep and not self._net_folder()))
        for cb in self.optional_boxes.values():
            if keep:
                cb.setChecked(False)
            cb.setEnabled(not keep)

    def _sync_warn_only(self) -> None:
        keep = self.keep_region_box.isChecked()
        region = fl.region_change(self.report)
        self.region_warn.setVisible((not keep) if region else (keep and not self._net_folder()))

    def _validation(self):
        from opx import arb

        return arb.saved_for(self.device.facts.get("serial", ""), self.rom.path.name)

    def _compare_typed_build(self, build: str):
        """The installed build as the owner typed it (phone in fastboot only), against this ROM."""
        from opx import arb

        v = arb.validate(self.rom, {"ro.build.display.id": build}, self.device.facts.get("serial", ""))
        v.detail += " " + tr("(Installed build as you typed it.)")
        arb.save(v)
        return v

    def _show_arb_validation(self) -> None:
        from opx import arb

        v = self._validation()
        if v is None:
            text, colour = tr("Not validated yet: Validate ROM / ARB compares this ROM with the build the phone runs."), "#f0c24b"
        else:
            text = tr("Validated:") + " " + v.detail
            colour = "#ff6b63" if v.result == arb.OLDER else "#9aa3b5"
        self.arb_status.setText(text)
        self.arb_status.setStyleSheet(f"color:{colour}")

    def _ask_arb(self) -> None:
        from opx.gui.arb_dialog import ArbRiskDialog

        validate = getattr(self.parent(), "validate_arb_now", None)
        dialog = ArbRiskDialog(self, self.arb_details, validation=self._validation(),
                               validate=(lambda: validate(self.rom)) if validate else None,
                               compare=self._compare_typed_build)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self._show_arb_validation()
        if accepted:
            self.accept_arb()

    def accept_arb(self) -> None:
        """The user went through the anti-rollback warning and accepted the risk."""
        self.arb_accepted = True
        if self.arb_btn is not None:
            self.arb_btn.setText(tr("Risk accepted"))
            self.arb_btn.setEnabled(False)
        self.refresh()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose your backup of this phone", self.backup_edit.text())
        if folder:
            self.backup_edit.setText(folder)

    def options(self) -> fl.FlashOptions:
        arb_ok = self.arb_accepted
        net = self._net_folder()
        restore = []
        if self.keep_region_box is not None and self.keep_region_box.isChecked():
            restore = fl.keepable(self.report, [n for n in self.device.profile.conditional
                                                if fl.backup_images(net, n)])
        return fl.FlashOptions(
            restore_from_backup=restore,
            network_backup=self.net_edit.text().strip() if self.net_edit is not None else "",
            keep_recovery=self.keep_recovery_box.isChecked(),
            no_backup=self.no_backup.isChecked(),
            include_optional=[p for p, cb in self.optional_boxes.items() if cb.isChecked()],
            wipe_data=self.wipe_box.isChecked(),
            reboot_after=self.reboot_box.isChecked(),
            allow_fastbootd=self.fastbootd_box.isChecked(),
            overrides=(list(fl.RISK_OVERRIDES) if arb_ok else [])
            + ([fl.OVERRIDE_VARIANT] if self.variant_box is not None and self.variant_box.isChecked() else [])
            + ([fl.OVERRIDE_UNKNOWN_FIRMWARE]
               if self.not_updated_box is not None and self.not_updated_box.isChecked() else []),
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
        lines += [f"Note: {w}" for w in self.plan.warnings]
        self.preview.setPlainText("\n".join(lines))
        if self.keep_region_box is not None:
            self._sync_warn_only()
        typed = self.confirm_edit.text().strip() == "FLASH"
        self.start_btn.setEnabled(self.plan.ok and typed)
