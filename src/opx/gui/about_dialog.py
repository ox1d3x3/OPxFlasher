"""Help > About OPxFlasher."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from opx import about
from opx.gui.icon import app_icon
from opx.i18n import tr


def _link(url: str, label: str = "") -> str:
    text = about.shown(label or url.split("://", 1)[-1])
    return f'<a href="{escape(url)}" style="color:#ff6a63">{escape(text)}</a>'


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("About OPxFlasher"))
        self.setFixedWidth(560)
        try:
            from PySide6 import __version__ as qt
        except ImportError:  # pragma: no cover
            qt = ""
        self.details = about.details(qt)

        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(72, 72))
        icon.setAlignment(Qt.AlignTop)
        head.addWidget(icon)
        title = QLabel(f"<div style='font-size:20pt; font-weight:700'>{about.APP}</div>"
                       f"<div style='color:#9aa3b5'>{escape(tr(about.TAGLINE))}</div>"
                       f"<div style='margin-top:6px'>{escape(tr('Version'))} <b>{escape(self.details['Version'])}</b></div>")
        head.addWidget(title, 1)
        layout.addLayout(head)

        rows = [
            (tr("Developer"), f"{escape(about.DEVELOPER)} ({_link(about.DEVELOPER_URL, '@' + about.HANDLE)})"),
            (tr("Project"), _link(about.REPO_URL)),
            (tr("Downloads"), _link(about.RELEASES_URL, tr("Releases"))),
            (tr("Help"), f"{_link(about.GUIDE_URL, tr('User guide'))} · {_link(about.CLI_URL, tr('Command line'))} · "
                         f"{_link(about.ISSUES_URL, tr('Report a problem'))}"),
            (tr("Support"), " · ".join(_link(url, name) for name, url in about.SUPPORT)),
            (tr("Licence"), f"{_link(about.LICENSE_URL, about.LICENSE_NAME)} · {_link(about.CREDITS_URL, tr('Credits'))}"),
        ]
        # a grid of plain labels: Qt sizes rich-text <table>s too short and clips rows
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        for i, (key, value) in enumerate(rows):
            name = QLabel(key)
            name.setStyleSheet("color:#9aa3b5")
            cell = QLabel(value)
            for lbl in (name, cell):  # rows never shrink below their text, at any display scaling
                lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            cell.setTextFormat(Qt.RichText)
            cell.setOpenExternalLinks(True)
            cell.setTextInteractionFlags(Qt.TextBrowserInteraction)
            grid.addWidget(name, i, 0, Qt.AlignTop)
            grid.addWidget(cell, i, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        system = QLabel(escape(tr("This copy")) + ": " + escape(
            f"Python {self.details['Python']}" + (f", Qt {self.details['Qt']}" if "Qt" in self.details else "")
            + f", {self.details['System']}, {self.details['Packaged']}"))
        system.setWordWrap(True)
        system.setTextInteractionFlags(Qt.TextSelectableByMouse)
        system.setStyleSheet("color:#9aa3b5")
        layout.addWidget(system)

        note = QLabel(escape(about.COPYRIGHT) + ". " + escape(tr(
            "Free software: you may share and change it under the GPL. It comes with no warranty; "
            "modifying phone firmware is at your own risk. Not affiliated with OnePlus or OPPO.")))
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa3b5; font-size:9pt")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        self.copy_btn = QPushButton(tr("Copy details"))
        self.copy_btn.setToolTip(tr("Copy the version and system details, for a bug report"))
        self.copy_btn.clicked.connect(self.copy_details)
        close = QPushButton(tr("Close"))
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self.adjustSize()  # height for the final width, fonts and scaling

    def copy_details(self) -> None:
        QGuiApplication.clipboard().setText(about.text(self.details.get("Qt", "")))
        self.copy_btn.setText(tr("Copied"))
