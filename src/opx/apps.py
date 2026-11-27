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
    location: str = ""  # where the APK lives: system, product, vendor, odm, my_*, data (user app)
    user_app: bool = False  # installed by the user (Play Store etc.), not part of the ROM


def check_package(name: str) -> str:
    if not (name == "android" or PKG_RE.match(name)) or len(name) > 255:
        raise AppError(f"Not a package name: {name!r}")
    return name


def keep_reason(package: str, extra_keep: list[str] | None = None) -> str:
    if package in KEEP or package.startswith(KEEP_PREFIXES):
        return "Needed by Android"
    for pattern in extra_keep or []:
        if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
            hit = pattern[1:-1] in package
        elif pattern.startswith("*"):
            hit = package.endswith(pattern[1:])
        elif pattern.endswith("*"):
            hit = package.startswith(pattern[:-1])
        else:
            hit = package == pattern
        if hit:
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


def _paths(adb: Tool, flags: str) -> dict[str, str]:
    """package -> APK path, from `pm list packages -f` ("package:/system/app/X/X.apk=com.x")."""
    res = adb.run("shell", f"pm list packages -f {flags}".strip(), timeout=60)
    if not res.ok:
        raise AppError(f"Couldn't list apps: {(res.stderr or res.stdout).strip()}")
    out = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:") and "=" in line:
            path, name = line[len("package:"):].rsplit("=", 1)
            if name == "android" or PKG_RE.match(name):
                out[name] = path
    return out


def location_of(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    if parts[0] == "data":
        return "data"
    if parts[0] == "system" and len(parts) > 1 and parts[1] in ("product", "system_ext", "vendor"):
        return parts[1]
    return parts[0]


def scan_apps(adb: Tool, extra_keep: list[str] | None = None, include_user: bool = True) -> list[App]:
    """Every app on the phone with where it lives and whether it may be removed. Works without root:
    `pm` runs as the shell user; removing is per user (`pm uninstall -k --user 0`) and reversible."""
    system = _paths(adb, "-s -u")
    installed = _packages(adb, "-s")
    disabled = _packages(adb, "-s -d")
    apps = [App(p, p not in installed, p in disabled, keep_reason(p, extra_keep), location_of(path))
            for p, path in sorted(system.items())]
    if include_user:
        user = _paths(adb, "-3")
        apps += [App(p, False, False, "", location_of(path) or "data", True)
                 for p, path in sorted(user.items()) if p not in system]
    return apps


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
    old, data = {}, {}
    try:
        data = json.loads(f.read_text("utf-8"))
        old = {e["package"]: e for e in data.get("removed", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        data = {}
    entries = [old.get(p) or {"package": p, "when": now} for p in packages]
    data = dict(data) if isinstance(data, dict) else {}
    data.update(serial=serial, removed=entries)  # keeps the recorded runs
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
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


BUILTIN_LISTS = Path(__file__).parent / "debloat"


def builtin_lists() -> list[tuple[str, str, Path]]:
    """The lists that ship with OPxFlasher: (title, one-line description, path)."""
    out = []
    for f in sorted(BUILTIN_LISTS.glob("*.txt")):
        head = [ln[1:].strip() for ln in f.read_text("utf-8").splitlines()[:2] if ln.startswith("#")]
        out.append((head[0] if head else f.stem, head[1] if len(head) > 1 else "", f))
    return out


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


# --- AI-assisted lists: export the phone's apps, import recommendations ------------------------

LEVELS = ("safe", "recommended", "aggressive", "keep")
DEFAULT_LEVELS = ("safe", "recommended")  # ticked after import; aggressive is the owner's choice

AI_PROMPT = """You are helping me debloat my Android phone ({model}, {build}, Android {android}).
The attached JSON (format "opxflasher-apps") lists every app on it: package, where it lives
(system / product / vendor / my_* = part of the ROM, data = installed by me), its state, and
"protected": true for apps OPxFlasher will never remove (don't suggest those).

Apps are removed only for my user (`pm uninstall -k --user 0`): reversible, nothing on the system
partition changes. Still, removing the wrong app can break calls, the camera, notifications,
updates or banking apps.

Reply with JSON only, in exactly this format (one entry per app you have an opinion on):

{{"format": "opxflasher-recommendations", "version": 1, "phone": "{model}",
  "packages": [
    {{"package": "com.example.app", "level": "safe", "reason": "what it is, why it can go"}}
  ]}}

level is one of:
- "safe": bloat or ads nobody needs; removing it breaks nothing
- "recommended": rarely used; removing it is fine for most people
- "aggressive": works, but some people use it (say what stops working)
- "keep": needed, or removing it breaks something (say what)
Only use package names from the list. When unsure, use "keep"."""


def inventory(apps: list[App], props: dict[str, str], rooted: bool) -> dict:
    """What `Export for AI` saves: the phone and its apps. No serial number, accounts or user data."""
    return {
        "format": "opxflasher-apps", "version": 1,
        "phone": {"model": props.get("ro.product.model", ""), "build": props.get("ro.build.display.id", ""),
                  "android": props.get("ro.build.version.release", ""), "rooted": rooted},
        "apps": [{"package": a.package, "location": a.location or ("data" if a.user_app else ""),
                  "user_app": a.user_app,
                  "state": "removed" if a.removed else "disabled" if a.disabled else "installed",
                  "protected": bool(a.keep), **({"why_protected": a.keep} if a.keep else {})}
                 for a in apps],
    }


def ai_prompt(props: dict[str, str]) -> str:
    return AI_PROMPT.format(model=props.get("ro.product.model", "phone") or "phone",
                            build=props.get("ro.build.display.id", "") or "?",
                            android=props.get("ro.build.version.release", "") or "?")


@dataclass
class Recommendation:
    package: str
    level: str = "recommended"
    reason: str = ""


def read_recommendations(path: Path) -> list[Recommendation]:
    """An AI's (or anyone's) answer: the JSON format from AI_PROMPT, a plain list of entries, or a
    text list (one package per line, like a debloat list). Package names are checked; nothing runs."""
    text = Path(path).read_text("utf-8-sig")
    stripped = text.strip()
    if stripped.startswith("```"):  # pasted straight from a chat
        stripped = stripped.strip("`")
        stripped = stripped[stripped.find("\n") + 1:] if "\n" in stripped else stripped
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise AppError(f"Not valid JSON: {e}") from e
        entries = data.get("packages", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise AppError("No \"packages\" list in that file.")
        out: dict[str, Recommendation] = {}
        for e in entries:
            if isinstance(e, str):
                e = {"package": e}
            if not isinstance(e, dict) or "package" not in e:
                continue
            name = check_package(str(e["package"]).strip())
            level = str(e.get("level", "recommended")).strip().lower()
            if level not in LEVELS:
                level = "keep"  # an answer we can't read is never a reason to remove something
            out[name] = Recommendation(name, level, str(e.get("reason", ""))[:500])
        return list(out.values())
    return [Recommendation(p) for p in read_list(Path(path))]


@dataclass
class Planned:
    package: str
    level: str
    reason: str
    status: str  # "remove", "protected", "not installed", "already removed", "keep"
    note: str = ""

    @property
    def removable(self) -> bool:
        return self.status == "remove"


def plan_recommendations(recs: list[Recommendation], apps: list[App]) -> list[Planned]:
    """Match recommendations against the phone. Protected apps are refused whatever the list says."""
    known = {a.package: a for a in apps}
    out = []
    for r in recs:
        a = known.get(r.package)
        if a is None:
            out.append(Planned(r.package, r.level, r.reason, "not installed", "not on this phone"))
        elif a.keep:
            out.append(Planned(r.package, r.level, r.reason, "protected", a.keep))
        elif a.removed:
            out.append(Planned(r.package, r.level, r.reason, "already removed"))
        elif r.level == "keep":
            out.append(Planned(r.package, r.level, r.reason, "keep"))
        else:
            out.append(Planned(r.package, r.level, r.reason, "remove"))
    order = {lv: i for i, lv in enumerate(LEVELS)}
    return sorted(out, key=lambda p: (not p.removable, order.get(p.level, 9), p.package))


# --- restore points: every debloat run is recorded, so it can be undone as a whole --------------


def record_run(serial: str, packages: list[str], source: str = "") -> None:
    if not packages:
        return
    f = _undo_file(serial)
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        data = {"serial": serial, "removed": []}
    runs = data.get("runs") if isinstance(data.get("runs"), list) else []
    runs.append({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
                 "packages": packages})
    data["runs"] = runs[-50:]
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(f)


def runs(serial: str) -> list[dict]:
    try:
        data = json.loads(_undo_file(serial).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data.get("runs", []) if isinstance(r, dict) and isinstance(r.get("packages"), list)]


def apply(adb: Tool, serial: str, packages: list[str], extra_keep: list[str] | None = None,
          source: str = "") -> tuple[list[str], list[str]]:
    """Remove these apps for user 0 and record the run. Returns (removed, problems)."""
    done, problems = [], []
    for p in packages:
        try:
            remove(adb, serial, p, extra_keep)
            done.append(p)
        except AppError as e:
            problems.append(str(e))
    record_run(serial, done, source)
    return done, problems


# --- OP Debloater (Ox1d3x3's root module, github.com/ox1d3x3/op-debloat) -------------------------
# Rooted phones can hand a list to the module instead: it removes systemlessly with its own state, keeps
# APK backups of user apps, re-applies after OTAs and has a boot-loop guard. Its import format is a JSON
# file {"format": "op-debloat", "schema": 1, ..., "packages": [...]} or a .list (one package per line).

OP_DEBLOAT_MODULE = "debloater_systemless_x1"
OP_DEBLOAT_DIR = "/sdcard/debloater"


def _safe_name(text: str) -> str:
    out = "".join(c if c.isalnum() or c in "_-" else "_" for c in text)[:40]
    return out or "list"


def op_debloat_json(packages: list[str], label: str, props: dict[str, str]) -> dict:
    from opx import __version__

    pkgs = sorted({check_package(p) for p in packages})
    return {"format": "op-debloat", "schema": 1, "app_version": f"OPxFlasher {__version__}",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": label,
            "device": props.get("ro.product.model", ""), "rom": props.get("ro.build.display.id", ""),
            "android": props.get("ro.build.version.release", ""), "count": len(pkgs), "packages": pkgs}


def op_debloat_name(label: str, props: dict[str, str]) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"op-debloat_{_safe_name(props.get('ro.product.model', '') or 'phone')}_{_safe_name(label)}_{stamp}"


def write_op_debloat(path: Path, packages: list[str], label: str, props: dict[str, str]) -> tuple[Path, Path]:
    """The module's own export format: <name>.json plus <name>.list (its profiles / v0.x lists)."""
    data = op_debloat_json(packages, label, props)
    path = Path(path).with_suffix(".json")
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    lst = path.with_suffix(".list")
    lst.write_text(f"# NAME: {label}\n" + "".join(p + "\n" for p in data["packages"]), "utf-8")
    return path, lst


def op_debloater_installed(adb: Tool) -> bool:
    """Is the module there (and root to run it)? Unrooted phones just answer no."""
    res = adb.run("shell", "su -c 'command -v debloater >/dev/null && echo opx-yes'", timeout=20)
    return "opx-yes" in res.stdout


def send_to_op_debloat(adb: Tool, packages: list[str], label: str, props: dict[str, str], workdir: Path,
                       run: bool = False) -> dict:
    """Copy the list to the phone where OP Debloater finds it (debloater/import, and as a custom profile so
    it shows in the WebUI). No root needed for that. With run=True (root + module), also apply it now with
    `debloater remove -f`; the module then owns the removal (undo, re-apply after OTA, boot-loop guard)."""
    name = op_debloat_name(label, props)
    js, lst = write_op_debloat(Path(workdir) / f"{name}.json", packages, label, props)
    profile_id = _safe_name(label)
    targets = {js: f"{OP_DEBLOAT_DIR}/import/{js.name}", lst: f"{OP_DEBLOAT_DIR}/profiles/{profile_id}.list"}
    adb.run("shell", f"mkdir -p {OP_DEBLOAT_DIR}/import {OP_DEBLOAT_DIR}/profiles", timeout=20)
    for local, remote in targets.items():
        res = adb.run("push", str(local), remote, timeout=60)
        if not res.ok:
            raise AppError(f"Couldn't copy {local.name} to the phone: {(res.stderr or res.stdout).strip()}")
    out = {"json": targets[js], "profile": f"custom:{profile_id}", "count": len(packages), "ran": False}
    if run:
        res = adb.run("shell", f"su -c 'debloater remove -f {targets[js]} --label {profile_id}'", timeout=600)
        text = (res.stdout + res.stderr).strip()
        if not res.ok:
            raise AppError(f"OP Debloater couldn't apply it: {text[-400:] or res.returncode}")
        out.update(ran=True, output=text[-2000:])
    return out
