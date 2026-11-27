"""Import recommendations (from an AI, a friend, a forum): see what each would do on this phone, tick, remove."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from opx import apps as ap
from opx.i18n import tr

OP_DEBLOATER = 2  # dialog result: send the ticked apps to the OP Debloater module

STATUS_TEXT = {"remove": "will be removed", "protected": "protected: never removed",
               "not installed": "not on this phone", "already removed": "already removed", "keep": "keep"}


class RecommendDialog(QDialog):
    def __init__(self, parent, planned: list[ap.Planned], source: str):
        super().__init__(parent)
        self.setWindowTitle(tr("Debloat recommendations"))
        self.resize(900, 640)
        self.planned = planned
        layout = QVBoxLayout(self)
        head = QLabel(f"<b>{source}</b><br>" + tr(
            "Ticked apps are removed for your user when you press Remove. Everything can be put back from the "
            "Apps tab (Restore last run / Restore all). Protected apps are never removed, whatever the list says."))
        head.setWordWrap(True)
        layout.addWidget(head)

        levels = QHBoxLayout()
        levels.addWidget(QLabel(tr("Tick:")))
        self.level_boxes: dict[str, QCheckBox] = {}
        for lv in ("safe", "recommended", "aggressive"):
            cb = QCheckBox(lv.capitalize())
            cb.setChecked(lv in ap.DEFAULT_LEVELS)
            cb.toggled.connect(self._levels_changed)
            self.level_boxes[lv] = cb
            levels.addWidget(cb)
        levels.addStretch(1)
        layout.addLayout(levels)

        self.table = QTableWidget(len(planned), 4)
        self.table.setHorizontalHeaderLabels([tr("Package"), tr("Level"), tr("On this phone"), tr("Why")])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for r, p in enumerate(planned):
            name = QTableWidgetItem(p.package)
            if p.removable:
                name.setFlags(name.flags() | Qt.ItemIsUserCheckable)
                name.setCheckState(Qt.Checked if p.level in ap.DEFAULT_LEVELS else Qt.Unchecked)
            else:
                name.setFlags(name.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEnabled)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(p.level))
            st = QTableWidgetItem(STATUS_TEXT.get(p.status, p.status) + (f" ({p.note})" if p.note
                                                                           and p.status == "protected" else ""))
            if not p.removable:
                st.setForeground(Qt.gray)
            self.table.setItem(r, 2, st)
            why = QTableWidgetItem(p.reason)
            why.setToolTip(p.reason)
            self.table.setItem(r, 3, why)
        self.table.itemChanged.connect(self._count)
        layout.addWidget(self.table, 1)

        self.summary = QLabel()
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.go = QPushButton()
        self.go.setObjectName("danger")
        self.go.clicked.connect(self._confirm)
        self.op_btn = QPushButton(tr("Send to OP Debloater..."))
        self.op_btn.setToolTip(tr("Rooted phones: hand the ticked apps to the OP Debloater module (systemless, "
                                  "re-applies after OTAs, boot-loop guard) instead of removing them here"))
        self.op_btn.clicked.connect(lambda: self.done(OP_DEBLOATER))
        row.addWidget(cancel)
        row.addWidget(self.op_btn)
        row.addWidget(self.go)
        layout.addLayout(row)
        self._count()

    def _levels_changed(self) -> None:
        on = {lv for lv, cb in self.level_boxes.items() if cb.isChecked()}
        self.table.blockSignals(True)
        for r, p in enumerate(self.planned):
            if p.removable:
                self.table.item(r, 0).setCheckState(Qt.Checked if p.level in on else Qt.Unchecked)
        self.table.blockSignals(False)
        self._count()

    def ticked(self) -> list[str]:
        return [p.package for r, p in enumerate(self.planned)
                if p.removable and self.table.item(r, 0).checkState() == Qt.Checked]

    def _count(self, *_):
        n = len(self.ticked())
        removable = sum(p.removable for p in self.planned)
        refused = sum(p.status == "protected" for p in self.planned)
        self.summary.setText(f"{n} of {removable} removable app(s) ticked" +
                             (f"; {refused} protected app(s) in the list are never removed." if refused else "."))
        self.go.setText(tr("Remove") + f" ({n})")
        self.go.setEnabled(n > 0)
        self.op_btn.setEnabled(n > 0)

    def _confirm(self) -> None:
        n = len(self.ticked())
        if QMessageBox.question(self, tr("Remove apps"),
                                f"Remove {n} app(s) for your user?\n\nThis is reversible: Apps tab > Restore last run. "
                                "Removing the wrong app can break a feature (calls, camera, notifications).") \
                == QMessageBox.Yes:
            self.accept()
