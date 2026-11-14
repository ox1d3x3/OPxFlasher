"""Bootloader unlock, step by step, with the phone's maker's own command.

Nothing here works around a lock: it runs ``fastboot flashing unlock`` (the
standard Android command; ``fastboot oem unlock`` on older OnePlus bootloaders),
and the phone itself asks the owner to confirm with its buttons. Unlocking wipes
the phone.

Steps:
  1. From Android: read whether it's already unlocked (verified boot state) and
     whether "OEM unlocking" is on in Developer options; stop if it's off.
  2. Reboot to the bootloader (never fastbootd; refused on firmware where that
     reboot is known to brick).
  3. ``getvar unlocked``: already unlocked -> nothing to do.
  4. Send the unlock command; the phone shows its confirmation screen.
  5. Wait for the owner to confirm on the phone (it then wipes and restarts).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from opx import reboot as rb
from opx.device import Device
from opx.fastboot import FastbootError, fastboot_serials, poll_interval, wait_for_fastboot
from opx.tools import Tool

# results
ALREADY = "already-unlocked"
NEEDS_OEM = "needs-oem-unlocking"
CONFIRM = "confirm-on-phone"  # command accepted; the owner must confirm on the phone
UNLOCKED = "unlocked"  # confirmed: the phone reports unlocked / is wiping
FAILED = "failed"

WARNINGS = (
    "Unlocking the bootloader lowers your phone's security: anyone with the phone in hand, or malware "
    "with root, can run unauthorised code on it, and verified boot can't protect you any more.",
    "Unlocking the bootloader may void your warranty, and some banking, payment and streaming apps "
    "may stop working.",
    "Unlocking the bootloader WIPES the phone: every app, photo, message and file on it is erased and "
    "can't be recovered. Back up anything you want to keep first.",
    "The OPxFlasher developer is not responsible for any damage, data loss or other consequences. "
    "You do this at your own risk.",
)

OEM_UNLOCK_STEPS = (
    "Turn on OEM unlocking first:\n"
    "  1. Settings > About phone (or About device > Version): tap the build number 7 times.\n"
    "  2. Settings > System (or Additional settings) > Developer options: turn on OEM unlocking.\n"
    "     If it's greyed out, connect to the internet, wait a little, or sign in to your account and retry.\n"
    "  3. Also turn on USB debugging there, then run this again."
)

CONFIRM_STEPS = (
    "Look at the phone now. Use the volume keys to choose \"UNLOCK THE BOOTLOADER\" and press the power "
    "button. The phone then erases itself and restarts; the first start takes a few minutes."
)


class UnlockError(RuntimeError):
    pass


@dataclass
class Result:
    status: str
    message: str


def _props(adb: Tool) -> dict[str, str]:
    res = adb.run("shell", "getprop", timeout=20)
    out = {}
    for line in res.stdout.splitlines():
        m = re.match(r"^\[(.+?)\]: \[(.*)\]$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def android_state(props: dict[str, str]) -> tuple[bool | None, bool | None]:
    """(unlocked, OEM unlocking allowed) as Android reports them; None = not reported."""
    locked = props.get("ro.boot.flash.locked")
    state = props.get("ro.boot.verifiedbootstate")
    unlocked = True if locked == "0" or state == "orange" else False if locked == "1" or state in ("green", "yellow") else None
    allowed = props.get("sys.oem_unlock_allowed")
    return unlocked, (allowed == "1") if allowed in ("0", "1") else None


def oppo_note(props: dict[str, str]) -> str:
    brand = (props.get("ro.product.brand") or props.get("ro.product.manufacturer") or "").lower()
    if brand in ("oppo", "realme"):
        return (" OPPO and realme phones usually also need the maker's in-depth test (Deep Testing) "
                "approval before the bootloader accepts the unlock command.")
    return ""


def run(device: Device, blocked: set[str] | None, adb: Tool | None = None, fastboot: Tool | None = None,
        say: Callable[[str], None] = print, wait_confirm: float = 300, cancel=None) -> Result:
    adb = (adb or Tool("adb")).bind(device.serial)
    fastboot = fastboot or Tool("fastboot")
    serial = device.serial

    # 1. what Android says
    if device.transport == "adb":
        if device.state != "device":
            raise UnlockError("Start the phone normally (Android, USB debugging on) and try again.")
        props = _props(adb)
        unlocked, allowed = android_state(props)
        if unlocked:
            return Result(ALREADY, "The bootloader is already unlocked. Nothing to do.")
        if allowed is False:
            return Result(NEEDS_OEM, "OEM unlocking is off on the phone." + oppo_note(props) + "\n\n" + OEM_UNLOCK_STEPS)
        if allowed is None:
            say("The phone doesn't report whether OEM unlocking is on; make sure it is (Developer options).")
        else:
            say("OEM unlocking is on.")
        # 2. to the bootloader (the reboot rules refuse it where it's known to brick)
        say("Rebooting to the bootloader...")
        rb.reboot(device, "bootloader", blocked, adb=adb)
        serial = wait_for_fastboot(fastboot, device.serial, userspace=False, timeout=120, cancel=cancel)
    elif device.transport == "fastboot":
        fb0 = fastboot.bind(serial)
        if fb0.run("getvar", "is-userspace", timeout=15).stderr.find("is-userspace: yes") >= 0:
            say("The phone is in fastbootd; restarting it to the bootloader...")
            fb0.run("reboot", "bootloader", timeout=30)
            serial = wait_for_fastboot(fastboot, serial, userspace=False, timeout=120, cancel=cancel)
    else:
        raise UnlockError("No phone connected.")

    fb = fastboot.bind(serial)
    # 3. already unlocked?
    if _unlocked(fb):
        return Result(ALREADY, "The bootloader is already unlocked. Nothing to do. (Use Reboot > Android to "
                               "start the phone.)")

    # 4. the maker's unlock command; the phone asks its owner
    say("Sending the unlock command (fastboot flashing unlock)...")
    res = fb.run("flashing", "unlock", timeout=60)
    text = (res.stdout + "\n" + res.stderr).lower()
    if res.returncode != 0 and ("unknown" in text or "not supported" in text or "unrecognized" in text):
        say("This bootloader uses the older command (fastboot oem unlock)...")
        res = fb.run("oem", "unlock", timeout=60)
        text = (res.stdout + "\n" + res.stderr).lower()
    if res.returncode != 0:
        if "not allowed" in text or ("oem unlock" in text and "disabled" in text) or "get_unlock_ability: 0" in text:
            return Result(NEEDS_OEM, "The bootloader refused: OEM unlocking isn't on.\n\n" + OEM_UNLOCK_STEPS
                          + "\n\nTo get back to Android: Reboot > Android.")
        detail = (res.stderr or res.stdout).strip().splitlines()
        return Result(FAILED, "The bootloader refused the unlock command: "
                      + (detail[-1] if detail else f"exit code {res.returncode}") + ".")
    say(CONFIRM_STEPS)

    # 5. wait for the owner to confirm on the phone
    deadline = time.monotonic() + wait_confirm
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            break
        if not fastboot_serials(fastboot):
            return Result(CONFIRM, "The phone left the bootloader. If you chose \"UNLOCK THE BOOTLOADER\", it is "
                                   "erasing itself and restarting unlocked (the first start takes a few minutes); "
                                   "if you chose not to unlock, nothing changed.")
        if _unlocked(fb):
            return Result(UNLOCKED, "The bootloader is now unlocked. The phone erases itself as it restarts; "
                                    "set it up again, then turn on USB debugging.")
        time.sleep(poll_interval())
    return Result(CONFIRM, CONFIRM_STEPS + "\n\nOPxFlasher didn't see the confirmation yet; check the phone's screen.")


def _unlocked(fb: Tool) -> bool:
    res = fb.run("getvar", "unlocked", timeout=20)
    return "unlocked: yes" in (res.stderr + res.stdout).lower()


__all__ = ["ALREADY", "CONFIRM", "FAILED", "NEEDS_OEM", "UNLOCKED", "WARNINGS", "FastbootError", "Result",
           "UnlockError", "run"]
