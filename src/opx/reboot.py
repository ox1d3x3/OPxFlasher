"""One-click reboots: Android, bootloader, fastbootd, recovery.

Works from Android or recovery (adb) and from the bootloader or fastbootd
(fastboot). Targets the firmware is known to break are refused: on builds whose
profile blocks "fastbootd" or "bootloader" (ColorOS 17 CN), those reboots are
the ones that bricked phones.
"""

from __future__ import annotations

from dataclasses import dataclass

from opx.device import Device
from opx.tools import Tool

TARGETS = {
    "system": "Android",
    "bootloader": "Bootloader (fastboot)",
    "fastbootd": "Fastbootd",
    "recovery": "Recovery",
}

_ADB = {"system": ("reboot",), "bootloader": ("reboot", "bootloader"),
        "fastbootd": ("reboot", "fastboot"), "recovery": ("reboot", "recovery")}
_FASTBOOT = {"system": ("reboot",), "bootloader": ("reboot", "bootloader"),
             "fastbootd": ("reboot", "fastboot"), "recovery": ("reboot", "recovery")}


class RebootError(RuntimeError):
    pass


@dataclass
class Choice:
    target: str
    allowed: bool
    reason: str = ""  # why it's refused, or a caution that needs confirming
    confirm: bool = False  # allowed, but ask first

    @property
    def label(self) -> str:
        return TARGETS[self.target]


def _current(device: Device) -> str:
    if device.transport == "adb":
        return {"device": "system", "recovery": "recovery"}.get(device.state, "")
    return {"bootloader": "bootloader", "fastbootd": "fastbootd"}.get(device.state, "")


def choices(device: Device | None, blocked: set[str] | None) -> list[Choice]:
    """What each reboot button should do for this phone.

    `blocked` is the profile's blocked actions for the phone's build, or None
    when the build can't be read (phone already in fastboot)."""
    out = []
    usable = device is not None and (
        (device.transport == "adb" and device.state in ("device", "recovery"))
        or (device.transport == "fastboot" and device.state in ("bootloader", "fastbootd"))
    )
    for target in TARGETS:
        if not usable:
            out.append(Choice(target, False, "No phone in Android, recovery or fastboot."))
            continue
        if target == _current(device):
            out.append(Choice(target, False, "The phone is already there."))
            continue
        if blocked and target in blocked:
            out.append(Choice(target, False,
                              f"Rebooting to {TARGETS[target].lower()} is known to brick phones on this firmware."))
            continue
        if blocked is None and target == "fastbootd":
            out.append(Choice(target, True, confirm=True, reason=(
                "OPxFlasher can't see the phone's build from fastboot. On ColorOS 17 CN, rebooting to "
                "fastbootd bricked phones. Only continue if your phone is NOT on ColorOS 17 CN.")))
            continue
        out.append(Choice(target, True))
    return out


def reboot(device: Device, target: str, blocked: set[str] | None,
           adb: Tool | None = None, fastboot: Tool | None = None) -> str:
    """Reboot `device` to `target`. Returns a short message."""
    if target not in TARGETS:
        raise RebootError(f"Unknown reboot target {target!r}.")
    choice = next(c for c in choices(device, blocked) if c.target == target)
    if not choice.allowed:
        raise RebootError(choice.reason)
    if device.transport == "adb":
        tool, args = (adb or Tool("adb")).bind(device.serial), _ADB[target]
    else:
        tool, args = (fastboot or Tool("fastboot")).bind(device.serial), _FASTBOOT[target]
    res = tool.run(*args, timeout=30)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise RebootError(f"Reboot failed: {msg[-1] if msg else res.returncode}")
    return f"Rebooting {device.serial} to {TARGETS[target].lower()}..."
