"""Custom recovery (AERA ...): link to the official page, load the file the owner downloaded, check its SHA-256."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from opx import customrec as cr
from opx.i18n import tr


class RecoveryDialog(QDialog):
    def __init__(self, parent, entries: list[tuple[str, dict[str, str]]], workdir: Path):
        """entries: (phone name, {name, url}); workdir: where a zip's image is extracted."""
        super().__init__(parent)
        self.setWindowTitle(tr("Install a custom recovery"))
        self.resize(720, 0)
        self.workdir = workdir
        self.loaded: cr.Loaded | None = None
        layout = QVBoxLayout(self)

        if entries:
            links = "<br>".join(f"{phone}: <a href='{e['url']}'>{e['name']} official download page</a>"
                                for phone, e in entries)
        else:
            links = tr("OPxFlasher doesn't know a custom recovery for this phone. Use the official page of the "
                       "recovery you want, and only one built for your exact phone.")
        intro = QLabel(
            "<b>" + tr("Why:") + "</b> " + tr(
                "on the OnePlus 13, AERA recovery gave the cleanest flashes (no USB drops in fastbootd), and it "
                "can install OPxFlasher recovery zips.") + "<br><br>"
            + "<b>1.</b> " + tr("Download it from the official page (OPxFlasher never downloads it for you):")
            + f"<br>{links}<br><br>"
            + "<b>2.</b> " + tr("Choose the zip you downloaded (or the recovery .img inside it).") + "<br>"
            + "<b>3.</b> " + tr("Compare the SHA-256 with the one on the download page (\"Verify SHA-256\")."))
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText(tr("The recovery zip or .img you downloaded"))
        row.addWidget(self.file_edit, 1)
        pick = QPushButton(tr("Choose file..."))
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        layout.addLayout(row)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.info)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("SHA-256 from the download page:")))
        self.sha_edit = QLineEdit()
        self.sha_edit.setPlaceholderText(tr("Paste it here to compare (recommended)"))
        self.sha_edit.textChanged.connect(self._compare)
        row.addWidget(self.sha_edit, 1)
        layout.addLayout(row)
        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)

        after = QLabel(tr("Next, the Flash image tab gets this image for 'recovery', both slots. Check and Flash "
                          "there as usual: the phone goes to the bootloader, which writes it (no fastbootd needed)."))
        after.setWordWrap(True)
        after.setStyleSheet("color:#9aa3b5")
        layout.addWidget(after)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.use_btn = QPushButton(tr("Use this recovery"))
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.accept)
        row.addWidget(self.use_btn)
        layout.addLayout(row)

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose the recovery you downloaded"), "",
                                              "Recovery (*.zip *.img);;All files (*)")
        if path:
            self.load(Path(path))

    def load(self, path: Path) -> None:
        self.file_edit.setText(str(path))
        self.loaded = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.loaded = cr.load(path, self.workdir)
        except cr.CustomRecError as e:
            self.info.setText(str(e))
            self.info.setStyleSheet("color:#f0a0a0")
        finally:
            QApplication.restoreOverrideCursor()
        if self.loaded:
            lo = self.loaded
            lines = [f"SHA-256 of {lo.source.name}: <code>{lo.source_sha256}</code>"]
            if lo.from_zip:
                lines.append(f"Recovery image found in the zip: {lo.member} "
                             f"({lo.image.stat().st_size / 1048576:.1f} MB)")
            else:
                lines.append(tr("You chose an .img: download pages usually give the zip's SHA-256, so compare "
                                "it only if the page lists the image's own."))
            self.info.setText("<br>".join(lines))
            self.info.setStyleSheet("")
        self._compare()

    def _compare(self, *_a) -> None:
        if not self.loaded:
            self.verdict.setText("")
            self.use_btn.setEnabled(False)
            return
        same = cr.matches(self.loaded, self.sha_edit.text())
        if same is None:
            self.verdict.setText(tr("Not compared. Paste the SHA-256 from the download page to be sure the file "
                                    "is the one they published."))
            self.verdict.setStyleSheet("color:#e0b060")
            self.use_btn.setEnabled(True)
        elif same:
            self.verdict.setText(tr("Matches the download page."))
            self.verdict.setStyleSheet("color:#7ad38a")
            self.use_btn.setEnabled(True)
        else:
            self.verdict.setText(tr("Does NOT match. Don't flash this file: download it again from the official "
                                    "page."))
            self.verdict.setStyleSheet("color:#f0a0a0")
            self.use_btn.setEnabled(False)
