""""Phone not showing up?": find OnePlus / OPPO phones the USB system sees but
adb / fastboot can't use, and say how to fix it.

- Windows: asks Windows' device list (PowerShell Get-PnpDevice) for phones by
  USB vendor ID and reports ones with a missing or broken driver.
- Linux: looks at /sys/bus/usb for phones and checks the user may open them
  (udev rules / plugdev group).
- Every OS: a phone in Qualcomm 9008 (EDL) mode is recognised and explained.
Read-only; nothing is installed or changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VENDORS = {"22D9": "OPPO / OnePlus", "2A70": "OnePlus", "18D1": "Android (fastboot)"}
QUALCOMM, EDL_PID = "05C6", "9008"
GOOGLE_DRIVER = "https://developer.android.com/studio/run/win-usb"

EDL_TEXT = ("A phone is in Qualcomm 9008 (EDL) mode. Neither adb nor fastboot can reach it, and "
            "OPxFlasher doesn't do EDL flashing. Try holding Power + Volume Up + Volume Down for "
            "about 15 seconds to force a restart; if it keeps returning to 9008, it needs an "
            "authorised EDL service.")


@dataclass
class Finding:
    level: str  # "ok", "warning", "problem"
    message: str


def _ids(instance: str) -> tuple[str, str]:
    up = instance.upper()
    vid = up.split("VID_", 1)[1][:4] if "VID_" in up else ""
    pid = up.split("PID_", 1)[1][:4] if "PID_" in up else ""
    return vid, pid


def windows_devices(run=subprocess.run) -> list[dict]:
    script = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_(22D9|2A70|18D1|05C6)' } | "
              "Select-Object Status, Class, FriendlyName, InstanceId | ConvertTo-Json -Compress")
    try:
        res = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = (res.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def check_windows(devices: list[dict]) -> list[Finding]:
    found = []
    phones = [d for d in devices if _ids(d.get("InstanceId", ""))[0] in VENDORS]
    for d in devices:
        vid, pid = _ids(d.get("InstanceId", ""))
        if vid == QUALCOMM and pid == EDL_PID:
            found.append(Finding("problem", EDL_TEXT))
    broken = [d for d in phones if d.get("Status") not in ("OK",) or not d.get("Class") or d.get("Class") == "Other"]
    for d in broken:
        name = d.get("FriendlyName") or "Android device"
        mode = "fastboot / bootloader" if "bootloader" in name.lower() or _ids(d["InstanceId"])[0] == "18D1" else "adb"
        found.append(Finding("problem", (
            f"'{name}' is plugged in but has no working driver (status: {d.get('Status') or 'unknown'}), "
            f"so {mode} can't see it. Install the Google USB Driver ({GOOGLE_DRIVER}) or the OnePlus / OPPO "
            "USB driver, then in Device Manager right-click the device -> Update driver -> Browse -> "
            "Let me pick -> 'Android ADB Interface' (Android) or 'Android Bootloader Interface' (fastboot). "
            "Unplug and plug the phone back in.")))
    if phones and not broken:
        found.append(Finding("ok", "Windows has working drivers for the connected phone."))
    return found


def linux_devices(root: Path = Path("/sys/bus/usb/devices")) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for dev in root.iterdir():
        try:
            vid = (dev / "idVendor").read_text().strip().upper()
            pid = (dev / "idProduct").read_text().strip().upper()
            bus = int((dev / "busnum").read_text())
            num = int((dev / "devnum").read_text())
        except (OSError, ValueError):
            continue
        if vid in VENDORS or vid == QUALCOMM:
            out.append({"vid": vid, "pid": pid, "node": f"/dev/bus/usb/{bus:03d}/{num:03d}"})
    return out


def check_linux(devices: list[dict], can_open=lambda node: os.access(node, os.R_OK | os.W_OK)) -> list[Finding]:
    found = []
    for d in devices:
        if d["vid"] == QUALCOMM and d["pid"] == EDL_PID:
            found.append(Finding("problem", EDL_TEXT))
    phones = [d for d in devices if d["vid"] in VENDORS]
    blocked = [d for d in phones if not can_open(d["node"])]
    if blocked:
        found.append(Finding("problem", (
            "The phone is plugged in but your user isn't allowed to open it, so adb / fastboot can't see it. "
            "Install your distribution's android-udev rules (e.g. 'android-udev' or 'android-sdk-platform-tools-common'), "
            "or add a udev rule for vendors 22d9 / 2a70 / 18d1 with MODE=\"0660\" GROUP=\"plugdev\", add yourself to "
            "plugdev, then log out and in and re-plug the phone.")))
    elif phones:
        found.append(Finding("ok", "Linux sees the phone and you may open it."))
    return found


def check() -> list[Finding]:
    if sys.platform.startswith("win"):
        found = check_windows(windows_devices())
    elif sys.platform.startswith("linux"):
        found = check_linux(linux_devices())
    else:
        found = []
    if not found:
        found.append(Finding("warning", (
            "No OnePlus / OPPO phone is visible on USB. Use a data cable (not charge-only) and a USB port on the "
            "computer itself, unlock the phone, turn on USB debugging (Developer options), and accept the "
            "'Allow USB debugging' prompt. In fastboot, the phone should show the fastboot screen.")))
    return found
