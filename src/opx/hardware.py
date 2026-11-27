"""Which hardware a phone is: the owner says it once (per phone), OPxFlasher remembers it.

The model a phone reports isn't enough: a Chinese OnePlus 13 converted to Global reports CPH2653 but is
still PJZ110 hardware, and on Android 17 that decides how it's flashed (China hardware keeps its 16 abl /
engineering_cdt; see flash.HARDWARE_QUESTION). So the owner picks the hardware in the phone panel, and the
flash and convert windows use it instead of asking every time.
"""

from __future__ import annotations

from opx import settings

SETTINGS_KEY = "phone_hardware"

# code -> (label, flash mode). The models are the OnePlus 13's, as an example; the flash mode is what matters.
CHOICES: dict[str, tuple[str, str]] = {
    "cn": ("China (PJZ110 / PKX110 / PLK110)", "cn"),
    "glo": ("Global (CPH2653 / CPH2747)", "global"),
    "eu": ("Europe (CPH2653 EEA)", "global"),
    "in": ("India (CPH2649 / CPH2723)", "global"),
    "na": ("North America (CPH2655)", "global"),
}


def label(code: str) -> str:
    return CHOICES.get(code, ("Not set", ""))[0]


def flash_mode(code: str) -> str:
    """The FlashOptions.hardware value for a chosen hardware: "cn", "global", or "" (not chosen)."""
    return CHOICES.get(code, ("", ""))[1]


def get(serial: str | None) -> str:
    if not serial:
        return ""
    data = settings.get(SETTINGS_KEY, {})
    code = data.get(serial, "") if isinstance(data, dict) else ""
    return code if code in CHOICES else ""


def put(serial: str, code: str) -> None:
    if not serial:
        return
    data = settings.get(SETTINGS_KEY, {})
    data = dict(data) if isinstance(data, dict) else {}
    if code in CHOICES:
        data[serial] = code
    else:
        data.pop(serial, None)
    settings.put(SETTINGS_KEY, data)


def model_hint(model: str) -> str:
    """A starting guess from the model the phone reports (only a guess: converted phones report the new one)."""
    m = (model or "").upper()
    if m.startswith(("PJZ", "PKX", "PLK")):
        return "cn"
    if m.startswith("CPH2655"):
        return "na"
    if m.startswith(("CPH2649", "CPH2723")):
        return "in"
    return ""
