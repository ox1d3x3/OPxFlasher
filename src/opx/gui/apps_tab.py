"""Apps tab: remove system apps for your user (debloat), and put them back."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opx import apps as ap
from opx.i18n import tr


def _change(adb, serial, keep, remove: list[str], restore: list[str]) -> list[str]:
    problems = []
    for p in remove:
        try:
            ap.remove(adb, serial, p, keep)
        except ap.AppError as e:
            problems.append(str(e))
    for p in restore:
        try:
            ap.restore(adb, serial, p)
        except ap.AppError as e:
            problems.append(str(e))
    return problems


class AppsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.apps: list[ap.App] = []
        self.by_name: dict[str, ap.App] = {}
        self.loaded_for = None
        layout = QVBoxLayout(self)

        intro = QLabel(tr("Remove system apps you don't use. They're only removed for your user: "
                          "nothing on the system partition changes, you can put them back here at any "
                          "time, and a factory reset brings them all back. Apps Android needs are locked."))
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#9aa3b5")
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search package names"))
        self.search.textChanged.connect(self._fill)
        row.addWidget(self.search, 1)
        self.only_removed = QCheckBox(tr("Only removed"))
        self.only_removed.toggled.connect(self._fill)
        row.addWidget(self.only_removed)
        self.load_btn = QPushButton(tr("Load apps"))
        self.load_btn.clicked.connect(self.load)
        row.addWidget(self.load_btn)
        layout.addLayout(row)

        self.table = QTableWidget(0, 2)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels([tr("Package"), tr("State")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.remove_btn = QPushButton(tr("Remove selected"))
        self.remove_btn.clicked.connect(self.remove_selected)
        self.restore_btn = QPushButton(tr("Restore selected"))
        self.restore_btn.clicked.connect(self.restore_selected)
        self.restore_all_btn = QPushButton(tr("Restore all I removed"))
        self.restore_all_btn.clicked.connect(self.restore_all)
        self.import_btn = QPushButton(tr("Remove from list..."))
        self.import_btn.setToolTip(tr("Remove the apps in a shared debloat list (one package per line)"))
        self.import_btn.clicked.connect(self.remove_from_list)
        self.export_btn = QPushButton(tr("Save my list..."))
        self.export_btn.setToolTip(tr("Save what you removed as a list to share or reuse"))
        self.export_btn.clicked.connect(self.export_list)
        for b in (self.remove_btn, self.restore_btn, self.restore_all_btn, self.import_btn, self.export_btn):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh_buttons()

    # --- state -----------------------------------------------------------------

    def _live(self) -> bool:
        return self.win.session is not None and not self.win.busy

    def refresh_buttons(self) -> None:
        live = self._live()
        current = getattr(self.win.session, "serial", None)
        if self.apps and self.loaded_for != current and current is not None:
            self.apps, self.by_name, self.loaded_for = [], {}, None  # another phone: its list is stale
            self.table.setRowCount(0)
            self.status.setText("")
        loaded = live and self.loaded_for == current
        rows = self._selected()
        self.load_btn.setEnabled(live)
        self.remove_btn.setEnabled(loaded and any(not a.removed and not a.keep for a in rows))
        self.restore_btn.setEnabled(loaded and any(a.removed for a in rows))
        mine = ap.removed_by_us(self.win.session.serial) if self.win.session else []
        self.restore_all_btn.setEnabled(loaded and bool(mine))
        self.export_btn.setEnabled(live and bool(mine))
        self.import_btn.setEnabled(loaded)

    def _selected(self) -> list[ap.App]:
        out = []
        for r in sorted({i.row() for i in self.table.selectedIndexes()}):
            item = self.table.item(r, 0)
            app = self.by_name.get(item.text()) if item is not None else None
            if app is not None:
                out.append(app)
        return out

    def _fill(self) -> None:
        text = self.search.text().strip().lower()
        mine = set(ap.removed_by_us(self.win.session.serial)) if self.win.session else set()
        shown = [a for a in self.apps if (not text or text in a.package.lower())
                 and (not self.only_removed.isChecked() or a.removed)]
        self.table.setRowCount(len(shown))
        for r, a in enumerate(shown):
            name = QTableWidgetItem(a.package)  # plain text only: no Python objects inside Qt items
            if a.keep:
                name.setToolTip(a.keep)
            state = "Removed" if a.removed else ("Disabled" if a.disabled else "Installed")
            if a.removed and a.package in mine:
                state += " (by OPxFlasher)"
            if a.keep:
                state = f"Locked: {a.keep.lower()}"
            st = QTableWidgetItem(state)
            if a.removed:
                st.setForeground(Qt.gray)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, st)
        self.refresh_buttons()

    # --- actions ----------------------------------------------------------------

    def load(self) -> None:
        from opx.gui.app import Job

        s = self.win.session
        if s is None:
            return
        self.load_btn.setEnabled(False)
        self.status.setText(tr("Reading the app list..."))
        self.win.run_job(Job(ap.list_system_apps, s.adb, s.profile.keep_apps), self._loaded, self._failed)

    def _loaded(self, apps) -> None:
        self.apps = apps
        self.by_name = {a.package: a for a in apps}
        self.loaded_for = self.win.session.serial if self.win.session else None
        removed = sum(a.removed for a in apps)
        self.status.setText(f"{len(apps)} system apps, {removed} removed for your user.")
        self._fill()

    def _failed(self, exc: BaseException) -> None:
        self.status.setText(str(exc))
        self.win.say(f"Apps: {exc}")
        self.refresh_buttons()

    def _run(self, remove: list[str], restore: list[str]) -> None:
        from opx.gui.app import Job

        s = self.win.session
        self.win._set_busy(True)
        self.status.setText(tr("Working..."))
        self.win.run_job(Job(_change, s.adb, s.serial, s.profile.keep_apps, remove, restore),
                         lambda problems: self._changed(problems, remove, restore), self._change_failed)

    def _changed(self, problems: list[str], remove: list[str], restore: list[str]) -> None:
        self.win._set_busy(False)
        for p in remove:
            self.win.say(f"Removed {p} for your user")
        for p in restore:
            self.win.say(f"Restored {p}")
        for p in problems:
            self.win.say(f"Apps: {p}")
        if problems:
            QMessageBox.warning(self, tr("Apps"), "\n".join(problems))
        self.load()

    def _change_failed(self, exc: BaseException) -> None:
        self.win._set_busy(False)
        self._failed(exc)

    def remove_selected(self, _=None, packages: list[str] | None = None) -> None:
        packages = packages or [a.package for a in self._selected() if not a.removed and not a.keep]
        if not packages:
            return
        listed = "\n".join(packages[:25]) + ("\n..." if len(packages) > 25 else "")
        if QMessageBox.question(
                self, tr("Remove apps"),
                f"Remove {len(packages)} app(s) for your user?\n\n{listed}\n\n"
                "You can put them back with Restore. Removing the wrong app can break a feature "
                "(for example the camera or calls), so only remove apps you know.") != QMessageBox.Yes:
            return
        self._run(packages, [])

    def restore_selected(self) -> None:
        packages = [a.package for a in self._selected() if a.removed]
        if packages:
            self._run([], packages)

    def restore_all(self) -> None:
        packages = ap.removed_by_us(self.win.session.serial)
        if packages:
            self._run([], packages)

    def remove_from_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose a debloat list"), "", "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            wanted = ap.read_list(Path(path))
        except (OSError, UnicodeDecodeError, ap.AppError) as e:
            QMessageBox.warning(self, tr("Apps"), f"Can't use that list: {e}")
            return
        known = {a.package: a for a in self.apps}
        todo = [p for p in wanted if p in known and not known[p].removed and not known[p].keep]
        skipped = len(wanted) - len(todo)
        if skipped:
            self.win.say(f"Debloat list: {skipped} package(s) skipped (not on this phone, already removed or locked)")
        if not todo:
            QMessageBox.information(self, tr("Apps"), tr("Nothing in that list can be removed on this phone."))
            return
        self.remove_selected(packages=todo)

    def export_list(self) -> None:
        s = self.win.session
        path, _ = QFileDialog.getSaveFileName(self, tr("Save debloat list"), "debloat.txt", "Text files (*.txt)")
        if not path:
            return
        ap.write_list(Path(path), ap.removed_by_us(s.serial), f"Removed on {s.props.get('ro.product.model', '')}")
        self.win.say(f"Debloat list saved: {path}")
