"""Restore a whole ROM from a Full firmware backup (offline OS backup).

A full backup is turned into a ROM made of its own image files (nothing is
copied): the partitions of the slot that was active when the backup was taken,
plus `super` (which holds system, vendor, product, my_region, ...). That ROM
then goes through the normal ROM check and flash engine, so every existing
gate applies: phone and variant, known bad firmware, security patch, AVB and
Qualcomm anti-rollback, sizes, never-flash partitions, journal, re-hash before
writing, super.img from the bootloader (no fastbootd).

Extra rules for restores:
- Only the phone the backup came from (serial must match).
- Personal data is never part of a backup, so it is never restored.
- Restoring an older build than the one installed needs a data wipe.
- If the phone can't boot, OPxFlasher can't read its current firmware to
  compare anti-rollback versions. The user must confirm they haven't installed
  an update since the backup (a newer firmware's anti-rollback can hard-brick
  a phone that gets older firmware).
"""

from __future__ import annotations

import json
from pathlib import Path

from opx.flash import OVERRIDE_UNKNOWN_FIRMWARE  # noqa: E402,F401  (re-exported)
from opx.plan import BLOCK, Check, DeviceState, Report
from opx.plan import sort_checks as pn_sort
from opx.profile import is_user_data
from opx.rom.ota import RomImage, RomInfo


class RestoreError(ValueError):
    pass


def manifest_of(folder: Path) -> dict:
    try:
        return json.loads((Path(folder) / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise RestoreError(f"{folder} is not an OPxFlasher backup (no readable manifest.json).") from e


def backup_as_rom(folder: Path) -> RomInfo:
    """A ROM view of a Full firmware backup, using the backed-up image files."""
    folder = Path(folder)
    manifest = manifest_of(folder)
    if manifest.get("mode") != "full":
        raise RestoreError("Only a Full firmware backup can restore the whole ROM "
                           f"(this one is '{manifest.get('mode')}').")
    if not manifest.get("finished"):
        raise RestoreError("This backup didn't finish, so it can't restore the ROM.")
    facts = manifest.get("device", {})
    slot = facts.get("slot_suffix", "")
    other = {"_a": "_b", "_b": "_a"}.get(slot, "")

    images: list[RomImage] = []
    for part in manifest.get("partitions", []):
        if part.get("status") == "failed" or part.get("source") == "mapper":
            continue
        name = part["name"]
        if is_user_data(name):
            continue  # never in a backup, but never restore it either
        if slot and name.endswith(slot):
            base = name[: -len(slot)]
        elif other and name.endswith(other):
            continue  # the slot that wasn't running when the backup was taken
        elif name == "super":
            base = name
        else:
            continue  # partitions without a slot are identity / calibration data
        images.append(RomImage(name=base, size=part["size"], logical=False, file=folder / part["file"]))
    if not any(i.name == "super" for i in images):
        raise RestoreError("This backup has no super.img, so it can't restore system, vendor and the rest.")

    info = RomInfo(path=folder, kind="backup", images=sorted(images, key=lambda i: i.name))
    info.metadata = {
        "pre-device": facts.get("codename", ""),
        "post-build": facts.get("fingerprint", ""),
        "post-security-patch-level": facts.get("security_patch", ""),
        "post-display-id": facts.get("build", ""),
    }
    if facts.get("build_date"):
        info.metadata["post-timestamp"] = facts["build_date"]
    info.notes.append(f"Full backup of {facts.get('model', '?')} {facts.get('build', '')}, "
                      f"taken {manifest.get('created_utc', '?')} from slot {slot.lstrip('_') or '?'}.")
    return info


def restore_checks(folder: Path, device: DeviceState, live: bool, wipe: bool) -> list[Check]:
    """Checks on top of the normal ROM report."""
    facts = manifest_of(folder).get("device", {})
    checks: list[Check] = []
    b_serial, d_serial = facts.get("serial", ""), device.facts.get("serial", "")
    if not b_serial or not d_serial:
        checks.append(Check("Same phone", BLOCK, "Can't confirm this backup came from this phone (serial unknown)."))
    elif b_serial != d_serial:
        checks.append(Check("Same phone", BLOCK,
                            f"This backup is from another phone ({b_serial}); this phone is {d_serial}. "
                            "A full ROM backup only restores to the phone it came from."))
    else:
        checks.append(Check("Same phone", "pass", f"Backup and phone are both {d_serial}."))

    if live:
        b_patch, d_patch = facts.get("security_patch", ""), device.facts.get("security_patch", "")
        b_build, d_build = facts.get("build", ""), device.facts.get("build", "")
        if b_patch and d_patch and b_patch < d_patch and not wipe:
            checks.append(Check("Older build", BLOCK,
                                f"The backup ({b_build}, {b_patch}) is older than what's installed "
                                f"({d_build}, {d_patch}). Going back needs a data wipe."))
        elif b_build and d_build and b_build != d_build:
            checks.append(Check("Build", "info", f"Installed {d_build}; the backup is {b_build}."))
    else:
        checks.append(Check(
            "Current firmware", BLOCK,
            "OPxFlasher can't read the phone's current firmware (it isn't running Android), so it can't "
            "compare anti-rollback versions. If you installed an update after this backup, restoring it "
            "can hard-brick the phone. Only continue if you haven't updated since the backup."
            + (f" On {device.profile.name} an older build also blows an anti-rollback fuse that "
               "can't be repaired, not even over EDL." if device.profile.rollback_fuse else "")))
    return checks


def merge(report: Report, extra: list[Check], device: DeviceState, live: bool, folder: Path) -> Report:
    facts = manifest_of(folder).get("device", {})
    same_build = live and facts.get("fingerprint") and facts.get("fingerprint") == device.facts.get("fingerprint")
    checks = []
    for c in report.checks:
        if c.name.startswith("Qualcomm ARB") and c.status == BLOCK:
            if same_build:
                c = Check(c.name, "pass", "Same build as installed, so anti-rollback is unchanged.")
            elif not live:
                # comparing the backup with itself says nothing; "Current firmware" covers the risk
                c = Check(c.name, "info", "Can't compare: the phone isn't running Android.")
        checks.append(c)
    report.checks = pn_sort(extra + checks)
    return report


def device_in_fastboot(folder: Path, serial: str) -> DeviceState:
    """The phone isn't running Android: describe it with the backup's facts and
    the serial fastboot actually reports (so a different phone can't pass)."""
    from opx.plan import device_from_backup

    device = device_from_backup(folder)
    device.facts = {**device.facts, "serial": serial}
    return device


def prepare_restore_rom(folder: Path, device: DeviceState, live: bool, wipe: bool,
                        overrides: list[str], allow_fastbootd: bool = False):
    """(rom, report, plan) for restoring a full backup to `device`."""
    from opx import flash as fl
    from opx.plan import build_report

    rom = backup_as_rom(folder)
    report = merge(build_report(rom, device), restore_checks(folder, device, live, wipe), device, live, folder)
    own_region = [i.partition for i in report.items if i.action == "conditional" and rom.image(i.partition)]
    options = fl.FlashOptions(
        include_optional=own_region,  # the phone's own region files are part of its backup
        wipe_data=wipe,
        reboot_after=True,
        allow_fastbootd=allow_fastbootd,
        overrides=overrides,
    )
    plan = fl.prepare(rom, report, device, options, Path(folder))
    return rom, report, plan
