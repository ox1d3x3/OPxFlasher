"""OPxFlasher as an MCP server: lets an AI assistant (Claude Desktop, or any MCP client) look at the
phone's apps and debloat it, with the same guard rails as the Apps tab.

Start it with `opx mcp` (or `OPxFlasher.exe mcp`). It speaks MCP over stdio: JSON-RPC 2.0, one
message per line. No network, no other tools: only listing phones, reading apps, and removing /
restoring apps for the phone's user (reversible). Protected apps can't be removed whatever the
assistant asks; removing needs a preview first, and the apply call is marked destructive so the
client asks the user to approve it. Nothing here flashes, wipes or reboots anything.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import asdict

from opx import __version__
from opx import apps as ap

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREVIEW_SECONDS = 15 * 60

INSTRUCTIONS = (
    "OPxFlasher debloats an Android phone (OnePlus / OPPO / realme first) connected over adb. Apps are "
    "removed only for the phone's user (pm uninstall -k --user 0): reversible with restore_apps, nothing on "
    "the system partitions changes. Workflow: list_phones -> scan_apps -> decide (explain each app and what "
    "stops working) -> debloat_preview -> show the user the preview -> debloat_apply only after they agree. "
    "Apps marked protected can't be removed. When unsure about an app, keep it."
)


def _schema(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or [],
            "additionalProperties": False}


SERIAL = {"type": "string", "description": "adb serial; leave out when one phone is connected"}

TOOLS = [
    {"name": "list_phones", "description": "Phones connected over adb (Android with USB debugging allowed).",
     "inputSchema": _schema(), "annotations": {"readOnlyHint": True}},
    {"name": "phone_info", "description": "Model, build, Android version and root access of a phone.",
     "inputSchema": _schema({"serial": SERIAL}), "annotations": {"readOnlyHint": True}},
    {"name": "scan_apps",
     "description": "Every app on the phone: package, where it lives (system / product / vendor / my_* = part of "
                    "the ROM, data = installed by the user), state, and protected (never removable).",
     "inputSchema": _schema({"serial": SERIAL,
                             "include_user_apps": {"type": "boolean", "description": "also apps the user installed"},
                             "filter": {"type": "string", "description": "only packages containing this text"}}),
     "annotations": {"readOnlyHint": True}},
    {"name": "debloat_preview",
     "description": "Check a list of packages against the phone before removing anything: what would be removed, "
                    "what's protected, not installed or already removed. Returns a preview_id for debloat_apply. "
                    "Nothing is changed.",
     "inputSchema": _schema({
         "serial": SERIAL,
         "packages": {"type": "array", "items": {"type": "object", "properties": {
             "package": {"type": "string"},
             "level": {"type": "string", "enum": list(ap.LEVELS)},
             "reason": {"type": "string"}}, "required": ["package"]},
             "description": "apps to remove, each with a level and a short reason"}}, ["packages"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "debloat_apply",
     "description": "Remove the apps of a preview (from debloat_preview) for the phone's user. Reversible with "
                    "restore_apps. Only call this after the user has seen the preview and agreed.",
     "inputSchema": _schema({"preview_id": {"type": "string"}}, ["preview_id"]),
     "annotations": {"destructiveHint": True, "idempotentHint": True}},
    {"name": "send_to_op_debloater",
     "description": "Rooted phones with the OP Debloater module (github.com/ox1d3x3/op-debloat): copy a preview's "
                    "apps to the phone as an OP Debloater list (import folder + custom profile). With run=true the "
                    "module removes them now (systemless, re-applies after OTAs, undo from its WebUI). Only "
                    "run after the user agreed to the preview.",
     "inputSchema": _schema({"preview_id": {"type": "string"},
                             "run": {"type": "boolean", "description": "remove now with the module"},
                             "label": {"type": "string"}}, ["preview_id"]),
     "annotations": {"destructiveHint": True}},
    {"name": "list_removed",
     "description": "Apps OPxFlasher removed on this phone, and each debloat run (restore points).",
     "inputSchema": _schema({"serial": SERIAL}), "annotations": {"readOnlyHint": True}},
    {"name": "restore_apps",
     "description": "Put removed apps back for the phone's user: the given packages, the last debloat run, or all "
                    "apps OPxFlasher removed.",
     "inputSchema": _schema({"serial": SERIAL,
                             "packages": {"type": "array", "items": {"type": "string"}},
                             "last_run": {"type": "boolean"}, "all": {"type": "boolean"}}),
     "annotations": {"destructiveHint": False}},
]


class ToolError(Exception):
    pass


class Server:
    def __init__(self, adb=None):
        self._adb = adb
        self.previews: dict[str, tuple[float, str, list[str]]] = {}

    # --- phone access ---------------------------------------------------------------

    def adb(self):
        from opx.tools import Tool

        if self._adb is None:
            self._adb = Tool("adb")
        return self._adb

    def phones(self) -> list[dict]:
        from opx.device import parse_adb_devices

        res = self.adb().run("devices", "-l", timeout=15)
        return [{"serial": d.serial, "state": d.state, "model": d.model}
                for d in parse_adb_devices(res.stdout) if d.transport == "adb"]

    def session(self, serial: str | None):
        from opx.session import open_session

        ready = [p for p in self.phones() if p["state"] == "device"]
        if serial:
            if not any(p["serial"] == serial for p in ready):
                raise ToolError(f"Phone {serial} isn't connected in Android with USB debugging allowed.")
        elif len(ready) == 1:
            serial = ready[0]["serial"]
        elif not ready:
            raise ToolError("No phone connected in Android with USB debugging allowed (unlock it and accept the "
                            "USB debugging prompt).")
        else:
            raise ToolError("More than one phone is connected: say which (serial).")
        return open_session(serial, self.adb())

    # --- tools ----------------------------------------------------------------------------

    def call(self, name: str, args: dict) -> object:
        handler = getattr(self, "t_" + name, None)
        if handler is None or not any(t["name"] == name for t in TOOLS):
            raise ToolError(f"Unknown tool {name!r}.")
        return handler(**args)

    def t_list_phones(self) -> object:
        return {"phones": self.phones()}

    def t_phone_info(self, serial: str | None = None) -> object:
        s = self.session(serial)
        p = s.props
        return {"serial": s.serial, "model": p.get("ro.product.model", ""), "build": p.get("ro.build.display.id", ""),
                "android": p.get("ro.build.version.release", ""), "profile": s.profile.name,
                "root": s.access.kind}

    def t_scan_apps(self, serial: str | None = None, include_user_apps: bool = True, filter: str = "") -> object:
        s = self.session(serial)
        apps = ap.scan_apps(s.adb, s.profile.keep_apps, include_user=include_user_apps)
        if filter:
            apps = [a for a in apps if filter.lower() in a.package.lower()]
        return ap.inventory(apps, s.props, s.access.usable)

    def t_debloat_preview(self, packages: list, serial: str | None = None) -> object:
        s = self.session(serial)
        recs = []
        for e in packages:
            e = {"package": e} if isinstance(e, str) else dict(e)
            try:
                name = ap.check_package(str(e.get("package", "")).strip())
            except ap.AppError as err:
                raise ToolError(str(err)) from err
            level = str(e.get("level", "recommended")).lower()
            recs.append(ap.Recommendation(name, level if level in ap.LEVELS else "keep", str(e.get("reason", ""))))
        plan = ap.plan_recommendations(recs, ap.scan_apps(s.adb, s.profile.keep_apps))
        todo = [p.package for p in plan if p.removable]
        out = {"phone": s.props.get("ro.product.model", ""), "will_remove": todo,
               "not_removed": [asdict(p) for p in plan if not p.removable]}
        if todo:
            pid = secrets.token_hex(6)
            self.previews[pid] = (time.monotonic(), s.serial, todo)
            out["preview_id"] = pid
            out["next"] = ("Show the user this list and the reasons. Call debloat_apply with the preview_id only "
                           "after they agree. Everything can be put back with restore_apps.")
        return out

    def t_debloat_apply(self, preview_id: str) -> object:
        entry = self.previews.pop(preview_id, None)
        if entry is None or time.monotonic() - entry[0] > PREVIEW_SECONDS:
            raise ToolError("Unknown or expired preview_id: run debloat_preview again.")
        _, serial, todo = entry
        s = self.session(serial)
        done, problems = ap.apply(s.adb, s.serial, todo, s.profile.keep_apps, source="Claude / MCP")
        return {"removed": done, "problems": problems,
                "undo": "restore_apps with last_run=true puts these back"}

    def t_send_to_op_debloater(self, preview_id: str, run: bool = False, label: str = "Claude picks") -> object:
        import tempfile
        from pathlib import Path

        entry = self.previews.get(preview_id)
        if entry is None or time.monotonic() - entry[0] > PREVIEW_SECONDS:
            raise ToolError("Unknown or expired preview_id: run debloat_preview again.")
        _, serial, todo = entry
        s = self.session(serial)
        if run:
            status = ap.op_debloater_status(s.adb)
            if status != "ready":
                raise ToolError(ap.OP_STATUS_TEXT[status] + " Send without run, or use debloat_apply.")
        out = ap.send_to_op_debloat(s.adb, todo, label, s.props, Path(tempfile.mkdtemp(prefix="opx-mcp-")), run)
        if run:
            self.previews.pop(preview_id, None)
            out.pop("results", None)  # removed / skipped / failed say it per app
            out["undo"] = "OP Debloater's WebUI > Undo, or: " + ap.OP_DEBLOAT_UNDO
        return out

    def t_list_removed(self, serial: str | None = None) -> object:
        s = self.session(serial)
        return {"removed_by_opxflasher": ap.removed_by_us(s.serial), "runs": ap.runs(s.serial)}

    def t_restore_apps(self, serial: str | None = None, packages: list | None = None, last_run: bool = False,
                       all: bool = False) -> object:  # noqa: A002 (the tool's own argument name)
        s = self.session(serial)
        if all:
            todo = ap.removed_by_us(s.serial)
        elif last_run:
            r = ap.runs(s.serial)
            todo = list(r[-1]["packages"]) if r else []
        else:
            todo = [str(p) for p in packages or []]
        if not todo:
            raise ToolError("Nothing to restore: give packages, last_run=true or all=true.")
        done, problems = [], []
        for p in todo:
            try:
                ap.restore(s.adb, s.serial, p)
                done.append(p)
            except ap.AppError as e:
                problems.append(str(e))
        return {"restored": done, "problems": problems}

    # --- JSON-RPC ---------------------------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification (initialized, cancelled ...): no answer
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "opxflasher", "title": "OPxFlasher", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    value = self.call(str(params.get("name")), dict(params.get("arguments") or {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, indent=1)}],
                              "structuredContent": value if isinstance(value, dict) else {"result": value},
                              "isError": False}
                except (ToolError, ap.AppError, TypeError, OSError) as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
                except Exception as e:  # noqa: BLE001  a tool error, not a dead server
                    result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _stdio():
    """Binary stdin / stdout. The Windows build is a GUI app: when Claude Desktop starts it with pipes,
    Python may still have set sys.stdin / sys.stdout to None, so fall back to the raw handles."""
    try:
        return sys.stdin.buffer, sys.stdout.buffer
    except AttributeError:
        pass
    if os.name == "nt":
        import ctypes
        import msvcrt

        k = ctypes.windll.kernel32
        fds = [msvcrt.open_osfhandle(k.GetStdHandle(h), 0) for h in (-10, -11)]
        return os.fdopen(fds[0], "rb", buffering=0), os.fdopen(fds[1], "wb", buffering=0)
    return os.fdopen(0, "rb", buffering=0), os.fdopen(1, "wb", buffering=0)


def serve(stdin=None, stdout=None, server: Server | None = None) -> int:
    if stdin is None or stdout is None:
        stdin, stdout = _stdio()
    server = server or Server()
    for raw in iter(stdin.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply).encode("utf-8") + b"\n")
            stdout.flush()
    return 0


# --- Claude Desktop: where its config lives, and the entry that starts this server ---------------


def claude_config_path():
    from pathlib import Path

    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def server_command() -> dict:
    """How Claude Desktop starts OPxFlasher's server: the app itself with `mcp`, or the opx command."""
    import shutil

    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["mcp"]}
    opx = shutil.which("opx")
    if opx:
        return {"command": opx, "args": ["mcp"]}
    return {"command": sys.executable, "args": ["-m", "opx.cli", "mcp"]}


def config_snippet() -> str:
    return json.dumps({"mcpServers": {"opxflasher": server_command()}}, indent=2)


def add_to_claude_desktop() -> tuple:
    """Add (or update) the opxflasher entry in Claude Desktop's config. The old file is kept as .bak.
    Returns (config path, backup path or None)."""
    path = claude_config_path()
    data: dict = {}
    backup = None
    if path.exists():
        text = path.read_text("utf-8")
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:
            raise ValueError(f"{path} isn't valid JSON ({e}); fix it or add the entry by hand.") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path} doesn't hold a JSON object; add the entry by hand.")
        backup = path.with_name(path.name + ".bak")
        backup.write_text(text, "utf-8")
    servers = data.get("mcpServers")
    data["mcpServers"] = dict(servers) if isinstance(servers, dict) else {}
    data["mcpServers"]["opxflasher"] = server_command()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)
    return path, backup
