"""Tools > ROM catalog mirrors: where Official ROMs looks when the OTA catalog is down."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from opx import catalog
from opx.i18n import tr


class MirrorsDialog(QDialog):
    """`run_job(fn, done, failed)` runs `fn` off the UI thread (the main window's runner)."""

    def __init__(self, parent, run_job):
        super().__init__(parent)
        self.run_job = run_job
        self.pending = self.testing = ""
        self.setWindowTitle(tr("ROM catalog mirrors"))
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("Official ROMs asks these servers in order and uses the first one that answers. "
                          "Mirrors only list the ROMs; downloads still come from OPPO / OnePlus servers and "
                          "are checked against the MD5. Only add mirrors you trust."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self.list)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("Add mirror..."))
        self.test_btn = QPushButton(tr("Test"))
        self.remove_btn = QPushButton(tr("Remove"))
        close = QPushButton(tr("Close"))
        self.add_btn.clicked.connect(self.add)
        self.test_btn.clicked.connect(self.test)
        self.remove_btn.clicked.connect(self.remove)
        close.clicked.connect(self.accept)
        for b in (self.add_btn, self.test_btn, self.remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        mine = catalog.user_mirrors()
        for i, base in enumerate(catalog.sources(), 1):
            kind = (tr("OTA catalog") if base == catalog.API else
                    tr("built-in mirror") if base in catalog.MIRRORS else tr("your mirror"))
            item = QListWidgetItem(f"{i}.  {base}    ({kind})")
            item.setData(Qt.ItemDataRole.UserRole, base)
            item.setData(Qt.ItemDataRole.UserRole + 1, base in mine)
            self.list.addItem(item)
        self._update_buttons()

    def _current(self) -> tuple[str, bool]:
        item = self.list.currentItem()
        if item is None:
            return "", False
        return item.data(Qt.ItemDataRole.UserRole), bool(item.data(Qt.ItemDataRole.UserRole + 1))

    def _update_buttons(self, *_):
        base, mine = self._current()
        self.test_btn.setEnabled(bool(base))
        self.remove_btn.setEnabled(mine)

    def add(self) -> None:
        text, ok = QInputDialog.getText(self, tr("Add mirror"),
                                        tr("Mirror address (e.g. ota.example.com or https://host/api/ota.php):"))
        if not ok or not text.strip():
            return
        try:
            url = catalog.normalize_mirror(text)
        except catalog.CatalogError as e:
            QMessageBox.warning(self, tr("Add mirror"), str(e))
            return
        if url in catalog.sources():
            self.status.setText(tr("That server is already in the list."))
            return
        self.status.setText(tr("Checking") + f" {url} ...")
        self.add_btn.setEnabled(False)
        self.pending = url
        self.run_job(lambda: catalog.test_mirror(url), self._added, self._add_failed)

    # job results: bound methods, so Qt runs them on the UI thread
    def _added(self, n) -> None:
        self.add_btn.setEnabled(True)
        try:
            catalog.set_user_mirrors(catalog.user_mirrors() + [self.pending])
        except OSError as e:
            self.status.setText(str(e))
            return
        self.status.setText(f"{self.pending}: OK, {n} " + tr("latest builds listed. Added."))
        self.refresh()

    def _add_failed(self, exc) -> None:
        self.add_btn.setEnabled(True)
        self.status.setText(f"{self.pending}: {exc} " + tr("Not added."))

    def _tested(self, n) -> None:
        self.status.setText(f"{self.testing}: OK, {n} " + tr("latest builds listed."))

    def _test_failed(self, exc) -> None:
        self.status.setText(f"{self.testing}: {exc}")

    def test(self) -> None:
        base, _ = self._current()
        if not base:
            return
        self.status.setText(tr("Checking") + f" {base} ...")
        self.testing = base
        self.run_job(lambda: catalog.test_mirror(base), self._tested, self._test_failed)

    def remove(self) -> None:
        base, mine = self._current()
        if mine:
            catalog.set_user_mirrors([u for u in catalog.user_mirrors() if u != base])
            self.status.setText(tr("Removed") + f" {base}.")
            self.refresh()
