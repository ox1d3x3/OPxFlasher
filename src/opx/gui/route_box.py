"""The "How to flash" choice at the top of the flash window, made to be seen.

Two big cards (A: from the PC, B: inside the custom recovery), the chosen one lit up, a NEW badge on B, and a
glowing border that pulses a few times when the window opens, so nobody scrolls past the choice.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout

from opx.i18n import tr

BLUE = QColor("#3d8bff")
GLOW = QColor("#b3d4ff")
PULSES = 3

STYLE = """
QFrame#routeBox { border: 2px solid %(border)s; border-radius: 12px;
                  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(61,139,255,%(bg)d),
                              stop:1 rgba(61,139,255,8)); }
QLabel#routeTitle { color: #ffffff; font-size: 15px; font-weight: 700; background: transparent; border: none; }
QLabel#routeSub { color: #b9c8e6; background: transparent; border: none; }
QFrame#routeCard { border: 1px solid #34405a; border-radius: 10px; background: rgba(255,255,255,6); }
QFrame#routeCard:hover { border-color: #5b7fb8; }
QFrame#routeCard[selected="true"] { border: 2px solid #3d8bff; background: rgba(61,139,255,38); }
QFrame#routeCard[blocked="true"] { border-style: dashed; }
QFrame#routeCard QRadioButton { font-size: 14px; font-weight: 700; color: #ffffff; background: transparent; }
QFrame#routeCard QRadioButton:disabled { color: #7d869a; }
QFrame#routeCard QLabel { background: transparent; border: none; }
QFrame#routeCard QLabel#routeBadge { color: #0b1a33; background: #7fb6ff; border-radius: 8px; padding: 1px 8px;
                    font-size: 11px; font-weight: 800; }
QPushButton#routeInstall { background: #3d8bff; border: 1px solid #3d8bff; color: white; font-weight: 700;
                           padding: 6px 14px; border-radius: 8px; }
QPushButton#routeInstall:hover { background: #5a9dff; border-color: #5a9dff; }
"""


def _mix(a: QColor, b: QColor, t: float) -> str:
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t)).name()


class RouteCard(QFrame):
    """One route: its radio button, what it does, and why it's unavailable (if it is). A click anywhere picks it."""

    def __init__(self, radio: QRadioButton, text: str, badge: str = "", blocked: str = ""):
        super().__init__()
        self.setObjectName("routeCard")
        self.radio = radio
        self.setCursor(Qt.CursorShape.PointingHandCursor if not blocked else Qt.CursorShape.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(radio)
        if badge:
            b = QLabel(badge)
            b.setObjectName("routeBadge")
            top.addWidget(b)
        top.addStretch(1)
        lay.addLayout(top)
        note = QLabel(text)
        note.setWordWrap(True)
        note.setStyleSheet("color:#aab3c5; margin-left:24px")
        lay.addWidget(note)
        if blocked:
            radio.setEnabled(False)
            why = QLabel(tr("Not possible now:") + " " + blocked)
            why.setWordWrap(True)
            why.setStyleSheet("color:#f0c24b; margin-left:24px")
            lay.addWidget(why)
        self.setProperty("blocked", bool(blocked))
        radio.toggled.connect(self._sync)
        self._sync()

    def _sync(self, *_):
        self.setProperty("selected", self.radio.isChecked())
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):  # noqa: N802 (Qt)
        if self.radio.isEnabled():
            self.radio.setChecked(True)
        super().mousePressEvent(event)


class RouteBox(QFrame):
    install_recovery = Signal()

    def __init__(self, rec_name: str, blocker: str = ""):
        super().__init__()
        self.setObjectName("routeBox")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(tr("Choose how to flash"))
        title.setObjectName("routeTitle")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)
        sub = QLabel(tr("Two ways to get this ROM onto the phone. Pick one before you go on."))
        sub.setObjectName("routeSub")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self.route_pc = QRadioButton(tr("A. From the PC (fastboot / fastbootd)"))
        self.route_rec = QRadioButton(tr("B. Inside the custom recovery") + f" ({rec_name})")
        self.card_pc = RouteCard(self.route_pc, tr(
            "The PC writes every partition over USB. Works with the stock recovery; changing region and wiping "
            "data are possible."))
        self.card_rec = RouteCard(self.route_rec, tr("OPxFlasher makes a recovery zip that keeps") + f" {rec_name}, "
                                  + tr("copies it to the phone (about 10 GB free needed) and checks it there. The "
                                       "phone restarts into the recovery and you press Install: the phone writes "
                                       "everything itself, the cable only carried the copy. Same variant only; your "
                                       "data stays."), badge=tr("NEW"), blocked=blocker)
        # one choice: the radios sit in different cards, so Qt doesn't make them exclusive by itself
        self.group = QButtonGroup(self)
        self.group.addButton(self.route_pc)
        self.group.addButton(self.route_rec)
        self.route_pc.setChecked(True)
        lay.addWidget(self.card_pc)
        lay.addWidget(self.card_rec)

        rec_row = QHBoxLayout()
        rec_tip = QLabel(tr("Recommended for both: install") + f" {rec_name} " + tr(
            "first (from its official page). It gave the cleanest flashes on the OnePlus 13. Skip it to keep the "
            "stock recovery (route A)."))
        rec_tip.setWordWrap(True)
        rec_tip.setStyleSheet("color:#cfd8ea; background: transparent; border: none")
        self.rec_btn = QPushButton(tr("Install") + f" {rec_name}...")
        self.rec_btn.setObjectName("routeInstall")
        self.rec_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rec_btn.setToolTip(tr("Opens the official download page and loads the file you downloaded (zip or "
                                   "recovery.img); written to recovery on both slots"))
        self.rec_btn.clicked.connect(self.install_recovery)
        rec_row.addWidget(rec_tip, 1)
        rec_row.addWidget(self.rec_btn)
        lay.addLayout(rec_row)

        # a glow that breathes a few times when the window opens, then settles
        self.pulse = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1300)
        self.pulse.setKeyValueAt(0.5, 1.0)
        self.pulse.setKeyValueAt(1.0, 0.0)
        self.pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.pulse.setLoopCount(PULSES)
        self.pulse.valueChanged.connect(self._paint)
        self._started = False
        self._paint(0.0)

    def _paint(self, t) -> None:
        t = float(t or 0.0)
        self.setStyleSheet(STYLE % {"border": _mix(BLUE, GLOW, t), "bg": int(22 + 40 * t)})

    def showEvent(self, event):  # noqa: N802 (Qt)
        super().showEvent(event)
        if not self._started:
            self._started = True
            self.pulse.start()
