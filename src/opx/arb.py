"""Qualcomm anti-rollback (ARB): the user's acknowledgement and the "Validate" comparison.

OPxFlasher can't always read the Qualcomm anti-rollback number (no root, or a reader not yet
validated for the phone). Flashing firmware with a LOWER number than the phone's blows a fuse and
bricks it, so that decision is left to the user, behind a serious warning (flash.ARB_WARNINGS), a
minute of reading, a small sum and an agreement.

`validate` helps with that decision: with the phone running Android it compares the ROM's OS version
with the installed one. Anti-rollback numbers only ever go up from build to build, so the same or a
newer build of the same phone keeps the number; an older build is where the danger is. Results are
remembered per phone (serial) and ROM, so the warning can show them later, e.g. with the phone in
fastboot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from opx import settings

NEWER, SAME, OLDER, UNKNOWN = "newer", "same", "older", "unknown"
SETTINGS_KEY = "arb_validations"

@dataclass
class Validation:
    serial: str
    rom: str  # ROM file / folder name
    rom_version: str
    installed: str  # installed build as Android reports it
    result: str  # NEWER / SAME / OLDER / UNKNOWN
    detail: str
    checked_utc: str = ""

    @property
    def ok(self) -> bool:
        return self.result in (NEWER, SAME)


def validate(rom, props: dict[str, str], serial: str) -> Validation:
    """Compare the ROM's OS version with the build the phone runs (props from Android, not a recovery)."""
    from opx.backup import device_facts
    from opx.plan import _int, is_recovery, os_version

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    name = rom.path.name
    rom_text = rom.metadata.get("post-display-id", "") or rom.metadata.get("post-build-incremental", "")
    if is_recovery(props):
        return Validation(serial, name, rom_text, "", UNKNOWN,
                          "The phone is in a custom recovery, which doesn't say what Android it runs. Start it "
                          "in Android (USB debugging on) and validate again.", now)
    installed = device_facts(props).get("build", "")
    rv, pv = os_version(rom_text), os_version(installed)
    if rv is not None and pv is not None:
        shown = f"ROM {'.'.join(map(str, rv))}, installed {'.'.join(map(str, pv))}"
        result = NEWER if rv > pv else SAME if rv == pv else OLDER
    else:
        rts, pts = _int(rom.metadata.get("post-timestamp", "")), _int(props.get("ro.build.date.utc", ""))
        if rts is None or pts is None:
            return Validation(serial, name, rom_text, installed, UNKNOWN,
                              "Can't tell this ROM's age or the installed build's: compare them yourself.", now)
        shown = "by build date"
        result = NEWER if rts > pts else SAME if rts == pts else OLDER
    detail = {
        NEWER: f"The ROM is NEWER than the installed build ({shown}): anti-rollback stays the same or goes up.",
        SAME: f"The ROM is the SAME build as the installed one ({shown}): anti-rollback stays the same.",
        OLDER: f"The ROM is OLDER than the installed build ({shown}). DON'T flash it: going back can blow the "
               "anti-rollback fuse and brick the phone.",
    }[result]
    return Validation(serial, name, rom_text, installed, result, detail, now)


def save(v: Validation) -> None:
    try:
        saved = settings.get(SETTINGS_KEY, {})
        saved = saved if isinstance(saved, dict) else {}
        saved[f"{v.serial}|{v.rom}"] = asdict(v)
        settings.put(SETTINGS_KEY, saved)
    except OSError:
        pass  # remembering is a convenience


def saved_for(serial: str, rom_name: str) -> Validation | None:
    data = settings.get(SETTINGS_KEY, {})
    entry = data.get(f"{serial}|{rom_name}") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        return Validation(**entry)
    except TypeError:
        return None
