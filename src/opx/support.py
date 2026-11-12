"""Everyday helpers: phone dashboard facts, log export, update check.

Nothing here runs in the background or sends anything on its own: the log is
saved where the user chooses (serial numbers masked), and the update check
asks GitHub's public releases list only when the user clicks it.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opx import __version__

RELEASES_API = "https://api.github.com/repos/ox1d3x3/OPxFlasher/releases?per_page=20"
RELEASES_PAGE = "https://github.com/ox1d3x3/OPxFlasher/releases"


# --- dashboard ------------------------------------------------------------------


def bootloader_text(props: dict[str, str]) -> str:
    state = props.get("ro.boot.verifiedbootstate", "")
    return {
        "orange": "Unlocked",
        "green": "Locked",
        "yellow": "Locked (custom key)",
        "red": "Verification failed",
    }.get(state, "Unknown")


@dataclass
class BackupInfo:
    folder: Path
    mode: str
    created: str
    finished: bool


def find_backups(root: Path, serial: str) -> list[BackupInfo]:
    """This phone's backups under `root` (one level deep), newest first."""
    out = []
    root = Path(root)
    if not serial or not root.is_dir():
        return out
    for manifest in root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("device", {}).get("serial") != serial:
            continue
        out.append(BackupInfo(manifest.parent, data.get("mode", "?"), data.get("created_utc", ""),
                              bool(data.get("finished"))))
    return sorted(out, key=lambda b: b.created, reverse=True)


def last_backup_text(backups: list[BackupInfo]) -> str:
    done = [b for b in backups if b.finished]
    if not done:
        return "None yet: make one before flashing"
    b = done[0]
    names = {"network": "Network", "critical": "Critical", "full": "Full"}
    return f"{names.get(b.mode, b.mode)}, {b.created[:16].replace('T', ' ')}"


# --- log export -----------------------------------------------------------------


def mask(text: str, serials: list[str]) -> str:
    """Hide serial numbers: keep the first and last 2 characters."""
    for s in sorted({s for s in serials if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, s[:2] + "*" * (len(s) - 4) + s[-2:])
    return text


def export_log(dest: Path, log_text: str, facts: dict[str, str] | None = None,
               serials: list[str] | None = None, journals: list[Path] | None = None) -> Path:
    """Write a support zip: app log, phone facts and recent journals, serials masked."""
    facts = dict(facts or {})
    serials = list(serials or []) + [facts.get("serial", "")]
    info = {
        "opxflasher": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phone": facts,
    }
    try:
        from PySide6 import __version__ as qt

        info["pyside6"] = qt
    except ImportError:
        pass
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("about.json", mask(json.dumps(info, indent=2), serials))
        zf.writestr("log.txt", mask(log_text, serials))
        for j in journals or []:
            try:
                zf.writestr(f"journals/{j.parent.name}-{j.name}", mask(j.read_text("utf-8"), serials))
            except OSError:
                continue
    return dest


# --- update check ---------------------------------------------------------------


def parse_version(tag: str) -> tuple:
    """'v0.7.0-rc.6' -> (0, 7, 0, 0, 6); a final '0.7.0' sorts after its candidates."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$", tag.strip())
    if not m:
        return ()
    major, minor, patch, rc = m.groups()
    return (int(major), int(minor), int(patch), 0 if rc else 1, int(rc or 0))


@dataclass
class Update:
    tag: str
    url: str
    prerelease: bool


def check_for_update(current: str = __version__, include_candidates: bool = True,
                     opener=None) -> Update | None:
    """The newest release newer than `current`, or None. Raises OSError when offline."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OPxFlasher/{__version__}",
    })
    try:
        with opener(req, timeout=15) as resp:
            releases = json.loads(resp.read())
    except (urllib.error.URLError, ValueError) as e:
        raise OSError(f"Couldn't reach GitHub: {e}") from e
    mine = parse_version(current)
    best = None
    for r in releases:
        if r.get("draft") or (r.get("prerelease") and not include_candidates):
            continue
        v = parse_version(r.get("tag_name", ""))
        if v and v > mine and (best is None or v > parse_version(best.tag)):
            best = Update(r["tag_name"], r.get("html_url") or RELEASES_PAGE, bool(r.get("prerelease")))
    return best
