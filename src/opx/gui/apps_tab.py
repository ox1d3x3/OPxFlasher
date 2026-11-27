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
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opx import apps as ap
from opx.i18n import tr


def _scan_all(adb, keep):
    return ap.scan_apps(adb, keep, include_user=True)


def _apply(adb, serial, keep, packages: list[str], source: str):
    done, problems = ap.apply(adb, serial, packages, keep, source=source)
    return problems


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
        self.import_btn.clicked.connect(lambda: self.remove_from_list())
        self.builtin_btn = QPushButton(tr("Built-in lists"))
        self.builtin_btn.setToolTip(tr("Debloat lists that come with OPxFlasher (drafts, reversible)"))
        menu = QMenu(self.builtin_btn)
        for title, about, path in ap.builtin_lists():
            act = menu.addAction(title)
            act.setToolTip(about)
            act.setStatusTip(about)
            act.triggered.connect(lambda _=False, p=path: self.remove_from_list(p))
        menu.setToolTipsVisible(True)
        self.builtin_btn.setMenu(menu)
        self.export_btn = QPushButton(tr("Save my list..."))
        self.export_btn.setToolTip(tr("Save what you removed as a list to share or reuse"))
        self.export_btn.clicked.connect(self.export_list)
        self.undo_run_btn = QPushButton(tr("Restore last run"))
        self.undo_run_btn.setToolTip(tr("Put back everything the last debloat run removed"))
        self.undo_run_btn.clicked.connect(self.restore_last_run)
        for b in (self.remove_btn, self.restore_btn, self.undo_run_btn, self.restore_all_btn, self.builtin_btn,
                  self.import_btn, self.export_btn):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)

        # AI-assisted: export the phone's apps, let an AI (or anyone) recommend, import and apply
        ai = QHBoxLayout()
        self.ai_export_btn = QPushButton(tr("Export for AI..."))
        self.ai_export_btn.setToolTip(tr("Save this phone's apps (no serial, no personal data) with a prompt to "
                                         "paste into any AI; it answers with a list you can import"))
        self.ai_export_btn.clicked.connect(self.export_for_ai)
        self.ai_import_btn = QPushButton(tr("Import recommendations..."))
        self.ai_import_btn.setToolTip(tr("Load an AI's answer (or any list): matching apps are ticked by level"))
        self.ai_import_btn.clicked.connect(lambda: self.import_recommendations())
        self.claude_btn = QPushButton(tr("Use with Claude Desktop..."))
        self.claude_btn.setToolTip(tr("Let Claude Desktop scan and debloat the phone through OPxFlasher (MCP)"))
        self.claude_btn.clicked.connect(self.claude_desktop)
        for b in (self.ai_export_btn, self.ai_import_btn, self.claude_btn):
            ai.addWidget(b)
        ai.addStretch(1)
        layout.addLayout(ai)
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
        self.builtin_btn.setEnabled(loaded)
        self.undo_run_btn.setEnabled(loaded and bool(ap.runs(self.win.session.serial)) if self.win.session else False)
        self.ai_export_btn.setEnabled(live)
        self.ai_import_btn.setEnabled(loaded)

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
        self.win.run_job(Job(ap.scan_apps, s.adb, s.profile.keep_apps, False), self._loaded, self._failed)

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

    def remove_from_list(self, path: Path | str | None = None) -> None:
        if not path:
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
        path, chosen = QFileDialog.getSaveFileName(
            self, tr("Save debloat list"), "debloat.txt",
            "Text files (*.txt);;OP Debloater (*.json)")
        if not path:
            return
        label = f"Removed on {s.props.get('ro.product.model', '')}"
        if path.lower().endswith(".json") or "OP Debloater" in chosen:
            js, lst = ap.write_op_debloat(Path(path), ap.removed_by_us(s.serial), label, s.props)
            self.win.say(f"OP Debloater list saved: {js} (+ {lst.name})")
            return
        ap.write_list(Path(path), ap.removed_by_us(s.serial), label)
        self.win.say(f"Debloat list saved: {path}")

    # --- AI-assisted ------------------------------------------------------------------

    def restore_last_run(self) -> None:
        r = ap.runs(self.win.session.serial)
        mine = set(ap.removed_by_us(self.win.session.serial))
        packages = [p for p in (r[-1]["packages"] if r else []) if p in mine]
        if packages:
            self._run([], packages)

    def export_for_ai(self) -> None:
        from opx.gui.app import Job

        s = self.win.session
        if s is None:
            return
        model = (s.props.get("ro.product.model") or "phone").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(self, tr("Export apps for AI"), f"{model}-apps.json", "JSON (*.json)")
        if not path:
            return
        self.status.setText(tr("Reading every app..."))

        def done(apps):
            import json

            from PySide6.QtWidgets import QApplication

            Path(path).write_text(json.dumps(ap.inventory(apps, s.props, s.access.usable), indent=1), "utf-8")
            prompt = ap.ai_prompt(s.props)
            Path(path).with_suffix(".prompt.txt").write_text(prompt, "utf-8")
            QApplication.clipboard().setText(prompt)
            self.status.setText(f"Saved {len(apps)} apps to {path}.")
            self.win.say(f"Apps exported for AI: {path} (prompt copied to the clipboard)")
            QMessageBox.information(
                self, tr("Export for AI"),
                f"Saved {Path(path).name} and {Path(path).with_suffix('.prompt.txt').name}.\n\n"
                "The prompt is on the clipboard: paste it into any AI chat, attach the JSON file, and save its answer "
                "as a .json file. Then press Import recommendations.")

        self.win.run_job(Job(_scan_all, s.adb, s.profile.keep_apps), done, self._failed)

    def import_recommendations(self, path: Path | str | None = None) -> None:
        from opx.gui.recommend_dialog import RecommendDialog

        if not path:
            path, _ = QFileDialog.getOpenFileName(self, tr("Choose recommendations"), "",
                                                  "Recommendations (*.json *.txt);;All files (*)")
            if not path:
                return
        try:
            recs = ap.read_recommendations(Path(path))
        except (OSError, UnicodeDecodeError, ap.AppError) as e:
            QMessageBox.warning(self, tr("Apps"), f"Can't use that file: {e}")
            return
        if not recs:
            QMessageBox.information(self, tr("Apps"), tr("No apps in that file."))
            return
        from opx.gui.recommend_dialog import OP_DEBLOATER

        planned = ap.plan_recommendations(recs, self.apps)
        dlg = RecommendDialog(self, planned, Path(path).name)
        result = dlg.exec()
        if result == OP_DEBLOATER:
            self.send_to_op_debloater(dlg.ticked(), Path(path).stem)
        elif result == RecommendDialog.Accepted:
            self._apply_run(dlg.ticked(), f"recommendations: {Path(path).name}")

    def send_to_op_debloater(self, packages: list[str], label: str) -> None:
        """Rooted phones: the OP Debloater module removes them (systemless, its own undo, re-apply after OTA)."""
        import tempfile

        from opx.gui.app import Job

        s = self.win.session
        if s is None or not packages:
            return
        status = ap.op_debloater_status(s.adb)
        installed = status == "ready"
        run = False
        if installed:
            run = QMessageBox.question(
                self, tr("OP Debloater"),
                f"OP Debloater is installed. Send {len(packages)} app(s) to it and remove them now?\n\n"
                "Yes: the module removes them (undo from its WebUI > Undo).\n"
                "No: only copy the list to the phone, to import in the module later.") == QMessageBox.Yes
        work = Path(tempfile.mkdtemp(prefix="opx-opdebloat-"))

        def done(out):
            where = f"{out['json']} and profile {out['profile']}"
            if out["ran"]:
                removed, skipped, failed = out["removed"], out["skipped"], out["failed"]
                text = f"OP Debloater removed {len(removed)} of {out['count']} app(s)."
                if skipped:
                    text += f"\n\nSkipped ({len(skipped)}):\n" + "\n".join(skipped[:25])
                if failed:
                    text += f"\n\nFailed ({len(failed)}):\n" + "\n".join(failed[:25])
                text += "\n\nUndo it from the module's WebUI (Undo) or with:\n" + ap.OP_DEBLOAT_UNDO
                self.win.say(f"OP Debloater: {len(removed)} removed, {len(skipped)} skipped, {len(failed)} failed; "
                             f"list at {where}\n{out.get('output', '')}")
                (QMessageBox.warning if failed or not removed else QMessageBox.information)(
                    self, tr("OP Debloater"), text)
                self.load()
            else:
                self.win.say(f"List copied for OP Debloater: {where}")
                QMessageBox.information(
                    self, tr("OP Debloater"),
                    f"Copied to the phone: {out['json']}\nIt's also the custom profile {out['profile']}.\n\n"
                    + ("Open OP Debloater's WebUI > Import (or Profiles) to apply it." if installed else
                       ap.OP_STATUS_TEXT[status] + " Then import this list in OP Debloater's WebUI."))

        self.win.run_job(Job(ap.send_to_op_debloat, s.adb, packages, label, s.props, work, run), done, self._failed)

    def _apply_run(self, packages: list[str], source: str) -> None:
        from opx.gui.app import Job

        if not packages:
            return
        s = self.win.session
        self.win._set_busy(True)
        self.status.setText(tr("Working..."))
        self.win.run_job(Job(_apply, s.adb, s.serial, s.profile.keep_apps, packages, source),
                         lambda problems: self._changed(problems, packages, []), self._change_failed)

    def claude_desktop(self) -> None:
        from PySide6.QtWidgets import QApplication

        from opx import mcp_server

        snippet = mcp_server.config_snippet()
        box = QMessageBox(self)
        box.setWindowTitle(tr("Use with Claude Desktop"))
        box.setText(tr("Claude Desktop can scan this phone's apps, explain them and debloat it through OPxFlasher "
                       "(debloat only: it can't flash, wipe or reboot). Protected apps stay protected, every "
                       "removal needs a preview, and Claude Desktop asks you before applying it."))
        box.setInformativeText(tr("Add OPxFlasher to Claude Desktop, then restart Claude Desktop. Config:") + "\n\n"
                               + snippet)
        add = box.addButton(tr("Add to Claude Desktop"), QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton(tr("Copy config"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(snippet)
            self.win.say("Claude Desktop config copied to the clipboard")
        elif box.clickedButton() is add:
            try:
                path, backup = mcp_server.add_to_claude_desktop()
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, tr("Claude Desktop"), str(e))
                return
            self.win.say(f"Added OPxFlasher to {path}" + (f" (old file kept as {backup.name})" if backup else ""))
            QMessageBox.information(self, tr("Claude Desktop"),
                                    f"Added to {path}.\n\nRestart Claude Desktop, connect the phone with USB "
                                    "debugging on, and ask Claude to look at your apps.")
