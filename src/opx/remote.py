"""Read-only commands that run on the phone over ADB.

Every command sent to the device is built in this module, from a small fixed
set of read-only programs (getprop, id, ls, blockdev, sha256sum and
``dd if=`` with no ``of=``). Partition names and paths are validated before
they are placed into a command string, so nothing from a partition table or
a profile can inject extra shell syntax.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from opx.tools import Result, Tool

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
PATH_RE = re.compile(r"^/dev/block/[A-Za-z0-9_./\-]+$")

BY_NAME_DIRS = ("/dev/block/by-name", "/dev/block/bootdevice/by-name")
MAPPER_DIR = "/dev/block/mapper"

DD_BLOCK = 4 * 1024 * 1024


class UnsafeCommand(ValueError):
    pass


def check_name(name: str) -> str:
    if not NAME_RE.match(name) or ".." in name:
        raise UnsafeCommand(f"Refusing unexpected partition name: {name!r}")
    return name


def check_path(path: str) -> str:
    if not PATH_RE.match(path) or ".." in path:
        raise UnsafeCommand(f"Refusing unexpected device path: {path!r}")
    return path


def _assert_read_only(cmd: str) -> None:
    tokens = cmd.split()
    if not tokens:
        raise UnsafeCommand("empty command")
    allowed = {"getprop", "id", "ls", "blockdev", "sha256sum", "dd", "readlink"}
    if tokens[0] not in allowed:
        raise UnsafeCommand(f"'{tokens[0]}' is not on the read-only allow list")
    if tokens[0] == "dd" and any(t.startswith("of=") for t in tokens):
        raise UnsafeCommand("dd with of= is never allowed in read-only mode")
    if tokens[0] == "blockdev" and tokens[1:2] != ["--getsize64"]:
        raise UnsafeCommand("only 'blockdev --getsize64' is allowed")
    for bad in (";", "&&", "||", "|", "`", "$(", ">", "<", "\n"):
        # 2>/dev/null is the only redirection we add, and only to dd.
        if bad in cmd.replace("2>/dev/null", ""):
            raise UnsafeCommand(f"shell metacharacter {bad!r} not allowed")


@dataclass
class Access:
    """How we get root on the device for reading partitions."""

    kind: str  # "root-shell" (recovery / adb root), "su" (Magisk/KSU/APatch) or "none"

    @property
    def usable(self) -> bool:
        return self.kind in ("root-shell", "su")


class RemoteShell:
    def __init__(self, adb: Tool, access: Access | None = None):
        self.adb = adb
        self.access = access or Access("none")

    def _wrap(self, cmd: str) -> str:
        _assert_read_only(cmd)
        if self.access.kind == "su":
            return f"su -c '{cmd}'"
        return cmd

    def shell(self, cmd: str, timeout: float = 30) -> Result:
        return self.adb.run("shell", self._wrap(cmd), timeout=timeout)

    def stream(self, cmd: str) -> subprocess.Popen:
        """Binary-safe output stream (adb exec-out, no pty)."""
        return self.adb.popen("exec-out", self._wrap(cmd))

    # --- probes -------------------------------------------------------

    def detect_access(self) -> Access:
        plain = self.adb.run("shell", "id -u", timeout=15)
        if plain.ok and plain.stdout.strip() == "0":
            self.access = Access("root-shell")
            return self.access
        su = self.adb.run("shell", "su -c 'id -u'", timeout=20)
        if su.ok and su.stdout.strip().splitlines()[-1:] == ["0"]:
            self.access = Access("su")
        else:
            self.access = Access("none")
        return self.access

    def getprops(self) -> dict[str, str]:
        res = self.adb.run("shell", "getprop", timeout=20)
        props: dict[str, str] = {}
        for line in res.stdout.splitlines():
            m = re.match(r"^\[(.+?)\]: \[(.*)\]$", line.strip())
            if m:
                props[m.group(1)] = m.group(2)
        return props

    def list_dir(self, path: str) -> list[str]:
        check_path(path)
        res = self.shell(f"ls -1 {path}")
        if not res.ok:
            return []
        return [n.strip() for n in res.stdout.splitlines() if n.strip() and NAME_RE.match(n.strip())]

    def size_of(self, path: str) -> int | None:
        check_path(path)
        res = self.shell(f"blockdev --getsize64 {path}")
        try:
            return int(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def sha256_of(self, path: str, timeout: float = 1800) -> str | None:
        check_path(path)
        res = self.shell(f"sha256sum {path}", timeout=timeout)
        if not res.ok:
            return None
        m = re.match(r"^([0-9a-fA-F]{64})\s", res.stdout.strip())
        return m.group(1).lower() if m else None

    def dd_stream(self, path: str) -> subprocess.Popen:
        check_path(path)
        return self.stream(f"dd if={path} bs={DD_BLOCK} 2>/dev/null")
