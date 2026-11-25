"""The animated phone at the top of the phone panel.

It shows at a glance what OPxFlasher sees: no phone (an unplugged cable drifting),
a phone being plugged in (the cable slides in, the screen lights up), and which mode
it's in (Android, bootloader, fastbootd, recovery, waiting for USB debugging, 9008).
Unplugging slides the cable out and the screen fades. Drawn with QPainter, no files.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

# state -> (screen colour, screen text, caption)
STATES = {
    "none": ("#2a2f3a", "", "No phone"),
    "checking": ("#2a2f3a", "", "Looking..."),
    "android": ("#3fb950", "Android", "Android"),
    "unauthorized": ("#d29922", "Allow\nUSB\ndebugging", "Waiting"),
    "offline": ("#d29922", "Offline", "Reconnect"),
    "recovery": ("#a371f7", "Recovery", "Recovery"),
    "sideload": ("#a371f7", "Sideload", "Recovery"),
    "bootloader": ("#f0883e", "FASTBOOT", "Bootloader"),
    "fastbootd": ("#58a6ff", "fastbootd", "fastbootd"),
    "edl": ("#f85149", "9008", "EDL (9008)"),
}


def state_for(device) -> str:
    """The animation state for a dev.Device (or None)."""
    if device is None:
        return "none"
    if device.transport == "fastboot":
        return "fastbootd" if device.state == "fastbootd" else "bootloader"
    return {"device": "android"}.get(device.state, device.state if device.state in STATES else "offline")


class PhoneAnim(QWidget):
    W, H = 220, 136

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.H)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.state = "none"
        self.plug = 0.0  # 0 = cable out, 1 = plugged in
        self.glow = 0.0  # screen brightness 0..1
        self.phase = 0.0  # idle motion
        self._from_colour = QColor(STATES["none"][0])
        self._plug_anim = QVariantAnimation(self, duration=550, easingCurve=QEasingCurve.Type.OutCubic)
        self._plug_anim.valueChanged.connect(self._set_plug)
        self._glow_anim = QVariantAnimation(self, duration=450, easingCurve=QEasingCurve.Type.InOutQuad)
        self._glow_anim.valueChanged.connect(self._set_glow)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._advance)
        self._tick.start(40)

    # --- state ----------------------------------------------------------------

    def set_state(self, state: str) -> None:
        state = state if state in STATES else "none"
        if state == self.state:
            return
        connected = state not in ("none", "checking")
        was = self.state not in ("none", "checking")
        self._from_colour = QColor(STATES[self.state][0])
        self.state = state
        if connected != was:
            self._animate(self._plug_anim, self.plug, 1.0 if connected else 0.0)
        self._glow_anim.stop()
        self._glow_anim.setStartValue(0.0 if connected else self.glow)
        self._glow_anim.setEndValue(1.0 if connected else 0.0)
        self._glow_anim.start()
        self.setToolTip(STATES[state][2])
        self.update()

    @staticmethod
    def _animate(anim: QVariantAnimation, start: float, end: float) -> None:
        anim.stop()
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.start()

    def _set_plug(self, v) -> None:
        self.plug = float(v)
        self.update()

    def _set_glow(self, v) -> None:
        self.glow = float(v)
        self.update()

    def _advance(self) -> None:
        if not self.isVisible():
            return
        self.phase = (self.phase + 0.06) % (2 * math.pi)
        self.update()

    # --- drawing --------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour, text, caption = STATES[self.state]
        accent = QColor(colour)
        cx = self.width() / 2
        phone = QRectF(cx - 30, 6, 60, 96)

        # halo while connected: a soft pulse in the mode's colour
        if self.state not in ("none", "checking"):
            pulse = 0.5 + 0.5 * math.sin(self.phase)
            halo = QColor(accent)
            halo.setAlphaF(0.10 + 0.12 * pulse * self.glow)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawRoundedRect(phone.adjusted(-7 - 3 * pulse, -5 - 3 * pulse, 7 + 3 * pulse, 5 + 3 * pulse), 16, 16)

        # body
        p.setPen(QPen(QColor("#4a5165"), 2))
        p.setBrush(QColor("#171a21"))
        p.drawRoundedRect(phone, 11, 11)
        # screen: blends from the old colour to the new one as it lights up
        screen = phone.adjusted(5, 9, -5, -9)
        lit = QColor(accent)
        dark = QColor("#0d0f14")
        mix = QColor(
            int(dark.red() + (lit.red() - dark.red()) * self.glow * 0.85),
            int(dark.green() + (lit.green() - dark.green()) * self.glow * 0.85),
            int(dark.blue() + (lit.blue() - dark.blue()) * self.glow * 0.85),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(screen, 6, 6)
        # camera dot
        p.setBrush(QColor("#4a5165"))
        p.drawEllipse(QPointF(cx, phone.top() + 5), 1.6, 1.6)

        if text and self.glow > 0.05:
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(6.5 if "\n" in text or len(text) >= 8 else 8.0)
            p.setFont(f)
            c = QColor("white")
            c.setAlphaF(min(1.0, self.glow * 1.2))
            p.setPen(c)
            p.drawText(screen, Qt.AlignmentFlag.AlignCenter, text)
        elif self.state in ("none", "checking"):
            # a slow "searching" sweep across the dark screen
            sweep = QColor("#8b949e")
            sweep.setAlphaF(0.25 + 0.2 * math.sin(self.phase * (2 if self.state == "checking" else 1)))
            p.setPen(QPen(sweep, 2, Qt.PenStyle.DotLine))
            y = screen.top() + (0.5 + 0.45 * math.sin(self.phase)) * screen.height()
            p.drawLine(QPointF(screen.left() + 6, y), QPointF(screen.right() - 6, y))

        # USB cable from below: slides up into the port when plugged in
        port_y = phone.bottom()
        gap = 18 * (1.0 - self.plug)
        drift = 0.0 if self.plug > 0.95 else 3 * math.sin(self.phase) * (1.0 - self.plug)
        plug_top = port_y + 2 + gap
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#8f98ab"))
        p.drawRoundedRect(QRectF(cx - 7 + drift, plug_top, 14, 9), 2, 2)
        cable = QPainterPath(QPointF(cx + drift, plug_top + 9))
        cable.cubicTo(QPointF(cx + drift, self.height() - 4), QPointF(cx + 40, self.height() - 2),
                      QPointF(cx + 90, self.height()))
        p.setPen(QPen(QColor("#5d6472"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(cable)

        # caption on the left
        f = QFont(self.font())
        f.setPointSizeF(9.0)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(colour) if self.state not in ("none", "checking") else QColor("#8b949e"))
        p.drawText(QRectF(0, phone.center().y() - 10, cx - 40, 20),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, caption)
        p.end()
