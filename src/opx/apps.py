"""Debloat: remove system apps for your user, and put them back.

Uses ``pm uninstall -k --user 0`` (the app stays on the system partition, it's
just removed for you) and ``cmd package install-existing --user 0`` to bring it
back. No root needed, nothing on the system partition changes, and a factory
reset brings everything back. Every removal is written to an undo list for
this phone so "Restore all" can undo it.

Apps the phone can't run without (system UI, settings, the phone app, the
package installer, ...) are never removed; a device profile can add more
with "keep_apps".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opx.tools import Tool

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Removing any of these can stop the phone booting to a usable screen.
KEEP = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.phone",
    "com.android.shell",
    "com.android.server.telecom",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.externalstorage",
    "com.android.inputdevices",
    "com.android.keychain",
    "com.android.location.fused",
    "com.android.se",
    "com.android.bluetooth",
    "com.android.nfc",
    "com.android.ons",
    "com.android.wallpaperbackup",
    "com.android.localtransport",
    "com.android.dynsystem",
    "com.android.devicelock",
}
KEEP_PREFIXES = (
    "com.android.providers.",
    "com.android.networkstack",
    "com.google.android.networkstack",
    "com.android.wifi.",
    "com.google.android.wifi.",
    "com.android.ims",
    "com.android.carrierconfig",
    "com.android.connectivity.",
    "com.google.android.connectivity.",
    "com.android.modulemetadata",
    "com.android.mtp",
)


class AppError(RuntimeError):
    pass


@dataclass
class App:
    package: str
    removed: bool      # removed for this user
    disabled: bool
    keep: str = ""     # why it can't be removed ("" = can be)


def check_package(name: str) -> str:
    if not (name == "android" or PKG_RE.match(name)) or len(name) > 255:
        raise AppError(f"Not a package name: {name!r}")
    return name


def keep_reason(package: str, extra_keep: list[str] | None = None) -> str:
    if package in KEEP or package.startswith(KEEP_PREFIXES):
        return "Needed by Android"
    for pattern in extra_keep or []:
        if package == pattern or (pattern.endswith("*") and package.startswith(pattern[:-1])):
            return "Needed by this phone (device profile)"
    return ""


def _packages(adb: Tool, flags: str) -> set[str]:
    res = adb.run("shell", f"pm list packages {flags}".strip(), timeout=60)
    if not res.ok:
        raise AppError(f"Couldn't list apps: {(res.stderr or res.stdout).strip()}")
    out = set()
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            name = line[len("package:"):]
            if name == "android" or PKG_RE.match(name):
                out.add(name)
    return out


def list_system_apps(adb: Tool, extra_keep: list[str] | None = None) -> list[App]:
    installed = _packages(adb, "-s")
    everything = _packages(adb, "-s -u")
    disabled = _packages(adb, "-s -d")
    return [App(p, p not in installed, p in disabled, keep_reason(p, extra_keep))
            for p in sorted(everything | installed)]


# --- undo list ----------------------------------------------------------------


def undo_dir() -> Path:
    from opx.tools import user_tools_dir

    return user_tools_dir().parent / "debloat"


def _undo_file(serial: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in serial) or "unknown"
    return undo_dir() / f"{safe}.json"


def removed_by_us(serial: str) -> list[str]:
    try:
        data = json.loads(_undo_file(serial).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [e["package"] for e in data.get("removed", []) if isinstance(e, dict) and "package" in e]


def _save_undo(serial: str, packages: list[str]) -> None:
    f = _undo_file(serial)
    f.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = {}
    try:
        old = {e["package"]: e for e in json.loads(f.read_text("utf-8")).get("removed", [])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    entries = [old.get(p) or {"package": p, "when": now} for p in packages]
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps({"serial": serial, "removed": entries}, indent=2), "utf-8")
    tmp.replace(f)


# --- remove / restore -------------------------------------------------------------


def remove(adb: Tool, serial: str, package: str, extra_keep: list[str] | None = None) -> None:
    check_package(package)
    why = keep_reason(package, extra_keep)
    if why:
        raise AppError(f"{package} can't be removed: {why.lower()}.")
    res = adb.run("shell", f"pm uninstall -k --user 0 {package}", timeout=60)
    if "Success" not in res.stdout:
        raise AppError(f"{package}: {(res.stdout + res.stderr).strip() or 'remove failed'}")
    done = removed_by_us(serial)
    if package not in done:
        _save_undo(serial, done + [package])


def restore(adb: Tool, serial: str, package: str) -> None:
    check_package(package)
    res = adb.run("shell", f"cmd package install-existing --user 0 {package}", timeout=60)
    text = (res.stdout + res.stderr).strip()
    if not res.ok or "installed for user" not in text:
        raise AppError(f"{package}: {text or 'restore failed'}")
    _save_undo(serial, [p for p in removed_by_us(serial) if p != package])


def read_list(path: Path) -> list[str]:
    """A shared debloat list: one package per line, '#' starts a comment."""
    out = []
    for raw in Path(path).read_text("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(check_package(line))
    return list(dict.fromkeys(out))


def write_list(path: Path, packages: list[str], title: str = "") -> Path:
    head = f"# {title}\n" if title else ""
    Path(path).write_text(head + "# OPxFlasher debloat list: one package per line\n"
                          + "".join(p + "\n" for p in packages), "utf-8")
    return Path(path)
