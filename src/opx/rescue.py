"""'My phone won't start': work out what state the phone is in and list what
to try, in order. Pure logic; the app and the CLI do the steps."""

from __future__ import annotations

from dataclasses import dataclass, field

from opx.device import Device

FORCE_RESTART = "Hold Power + Volume Up for about 10 seconds until the screen goes off, then let go."
TO_FASTBOOT = "With the phone off, hold Power + Volume Down until the fastboot screen appears."
LEAVE_9008 = "Hold Power + Volume Up + Volume Down for about 15 seconds to force a restart."


@dataclass
class Step:
    text: str
    action: str = ""   # "", "usb-check", "identify-9008", "reboot:<target>", "restore-rom",
                       # "restore-zip", "official-roms", "save-logs", "check-again"
    label: str = ""    # button text


@dataclass
class Assessment:
    state: str        # "edl", "bootloader", "fastbootd", "recovery", "bootloop", "android", "unauthorized", "none"
    title: str
    summary: str
    steps: list[Step] = field(default_factory=list)


def assess(devices: list[Device], edl_found: bool, has_full_backup: bool,
           boot_completed: str | None = None, blocked: set[str] | None = None) -> Assessment:
    """`boot_completed` is sys.boot_completed for a phone in Android ('' or None = unknown);
    `blocked` are reboot targets a known issue forbids on this build."""
    blocked = blocked or set()
    restore = (Step("Restore the whole ROM from your Full backup (same phone, same or newer build only).",
                    "restore-rom", "Restore full ROM...") if has_full_backup else
               Step("You have no Full backup of this phone here, so the way back is the official full ROM "
                    "for your build: download it, check it, then flash it.", "official-roms", "Official ROMs..."))
    adb = [d for d in devices if d.transport == "adb"]
    fb = [d for d in devices if d.transport == "fastboot"]

    if fb:
        state = fb[0].state  # bootloader / fastbootd
        steps = [Step("Sometimes a restart is all it needs.", "reboot:system", "Restart")]
        if "recovery" not in blocked:
            steps.append(Step("Try recovery: if it opens, you can restore from there or wipe data.",
                              "reboot:recovery", "Recovery"))
        steps.append(restore)
        if "bootloader" in blocked or "fastbootd" in blocked:
            steps.insert(0, Step("This phone's build has a known problem with fastboot reboots. Avoid "
                                 "'Reboot to fastbootd / bootloader' and use the steps below."))
        return Assessment(state, "The phone is in fastboot",
                          "It reached the bootloader, so it can be restored from the PC.", steps)

    rec = [d for d in adb if d.state in ("recovery", "sideload")]
    if rec:
        steps = [Step("Restart to see if Android starts.", "reboot:system", "Restart")]
        if has_full_backup:
            steps.append(Step("Restore from recovery: make a recovery zip from your Full backup, copy it to the "
                              "phone (or use adb sideload) and install it in OrangeFox / TWRP / AERA.",
                              "restore-zip", "Make recovery zip..."))
        steps.append(Step("Or go to fastboot and restore from the PC.", "reboot:bootloader", "Fastboot"))
        steps.append(Step("Save logs first if you want to report the problem.", "save-logs", "Save logs..."))
        return Assessment("recovery", "The phone is in recovery",
                          "Recovery works, so the phone can be restored.", steps)

    if any(d.state == "unauthorized" for d in adb):
        return Assessment("unauthorized", "Allow USB debugging",
                          "The phone is on but hasn't allowed this computer yet.",
                          [Step("Unlock the phone and tap 'Allow' on the USB debugging prompt.", "check-again",
                                "Check again")])

    if any(d.state == "device" for d in adb):
        if boot_completed == "1":
            return Assessment("android", "Android is running",
                              "The phone started normally. Nothing to rescue.",
                              [Step("While it works, make a Full firmware backup (Backup tab), so a bad flash "
                                    "can always be undone.")])
        return Assessment("bootloop", "Android is starting but not finishing",
                          "The phone talks over ADB but Android hasn't finished starting (boot loop?).",
                          [Step("Save the logs now: they show why it doesn't start.", "save-logs", "Save logs..."),
                           Step("Try recovery (wipe cache / data there if you accept losing data).",
                                "reboot:recovery", "Recovery"),
                           Step("Or go to fastboot and restore from the PC.", "reboot:bootloader", "Fastboot"),
                           restore])

    if edl_found:
        return Assessment("edl", "The phone is in 9008 (EDL) mode",
                          "Only the chip's emergency loader is running. adb and fastboot can't reach it.",
                          [Step(LEAVE_9008 + " It may then start, or show fastboot: check again after."),
                           Step(TO_FASTBOOT, "check-again", "Check again"),
                           Step("Read the phone's identification (read-only) for a service centre.",
                                "identify-9008", "Identify..."),
                           Step("If it keeps returning to 9008, it needs an authorised OnePlus / OPPO service "
                                "centre: EDL flashing needs OnePlus's own signed loader and authorisation. "
                                "Take the identification details and your backup.")])

    return Assessment("none", "No phone found",
                      "Nothing answers over USB: adb, fastboot and 9008 are all quiet.",
                      [Step("Check drivers, cable and port.", "usb-check", "USB check"),
                       Step(FORCE_RESTART),
                       Step(TO_FASTBOOT, "check-again", "Check again"),
                       Step("If the screen stays black with no vibration, charge it for 30 minutes and try "
                            "again.")])


def gather(backup_roots, adb=None, fastboot=None) -> Assessment:
    """Look at what's connected (read-only) and assess it."""
    from pathlib import Path

    from opx import device as dev
    from opx import support, usbcheck

    scan = dev.scan(adb, fastboot)
    edl = False
    try:
        from opx import sahara

        edl = bool(sahara.find_ports())
    except Exception:  # noqa: BLE001  (no serial support: fall back to the USB check)
        pass
    if not edl and not scan.devices:
        edl = any(f.message == usbcheck.EDL_TEXT for f in usbcheck.check())
    serials = {d.serial for d in scan.devices}
    has_full = any(b.finished and b.mode == "full"
                   for root in backup_roots for s in serials for b in support.find_backups(Path(root), s))
    boot, blocked = None, set()
    android = [d for d in scan.devices if d.transport == "adb" and d.state == "device"]
    if android:
        from opx.session import open_session

        try:
            s = open_session(android[0].serial, adb)
            boot = s.props.get("sys.boot_completed", "")
            blocked = s.profile.blocked_actions(s.props)
        except Exception:  # noqa: BLE001  (a half-started phone may not answer everything)
            pass
    return assess(scan.devices, edl, has_full, boot, blocked)
