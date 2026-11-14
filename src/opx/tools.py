"""Locating and running adb / fastboot.

Lookup order for each tool:
  1. OPX_ADB / OPX_FASTBOOT environment variables (a path, or a command
     line such as "python fake_adb.py" for testing)
  2. a ``platform-tools`` folder next to the application
  3. the per-user tools folder filled by ``opx tools fetch``
  4. the system PATH

Commands are always passed as argument lists, never through a local shell.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

GOOGLE_PLATFORM_TOOLS = {
    "win32": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

_EXE = ".exe" if os.name == "nt" else ""


class ToolNotFound(RuntimeError):
    pass


def user_tools_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "OPxFlasher" / "platform-tools"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):  # packaged build
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _candidates(name: str) -> list[Path]:
    dirs = [app_dir() / "platform-tools"]
    # An AppImage runs from a read-only mount; look next to the .AppImage file instead.
    if os.environ.get("APPIMAGE"):
        dirs.append(Path(os.environ["APPIMAGE"]).resolve().parent / "platform-tools")
    dirs.append(user_tools_dir())
    return [d / f"{name}{_EXE}" for d in dirs]


def missing_tools(names: tuple[str, ...] = ("adb", "fastboot")) -> list[str]:
    """Which of adb / fastboot can't be found (checked at start-up)."""
    out = []
    for name in names:
        try:
            find_tool(name)
        except ToolNotFound:
            out.append(name)
    return out


def find_tool(name: str) -> list[str]:
    """Return the command prefix for ``adb`` or ``fastboot``."""
    env = os.environ.get(f"OPX_{name.upper()}")
    if env:
        if os.name == "nt":  # posix=False keeps quotes around paths; drop them
            return [part.strip('"') for part in shlex.split(env, posix=False)]
        return shlex.split(env)
    for path in _candidates(name):
        if path.is_file():
            return [str(path)]
    found = shutil.which(name)
    if found:
        return [found]
    raise ToolNotFound(
        f"{name} not found. Run 'opx tools fetch' to download Google's platform-tools, "
        f"or put {name} on your PATH."
    )


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Tool:
    """A thin wrapper around adb or fastboot, optionally bound to one serial."""

    def __init__(self, name: str, serial: str | None = None, prefix: list[str] | None = None):
        self.name = name
        self.serial = serial
        self.prefix = prefix if prefix is not None else find_tool(name)

    def bind(self, serial: str) -> Tool:
        return Tool(self.name, serial, self.prefix)

    def argv(self, *args: str) -> list[str]:
        cmd = list(self.prefix)
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd + list(args)

    def run(self, *args: str, timeout: float = 30) -> Result:
        try:
            proc = subprocess.run(
                self.argv(*args),
                capture_output=True,
                timeout=timeout,
                **_no_window(),
            )
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{self.name} timed out after {timeout}s")
        return Result(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            self.argv(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_no_window(),
        )


def _no_window() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def fetch_platform_tools(dest: Path | None = None, progress=None) -> Path:
    """Download Google's official platform-tools into the per-user folder.

    The download is Google's Android SDK Platform-Tools and is covered by
    Google's terms; OPxFlasher does not redistribute it.
    """
    if sys.platform.startswith("win"):
        key = "win32"
    elif sys.platform == "darwin":
        key = "darwin"
    elif sys.platform.startswith("linux"):
        key = "linux"
    else:
        raise RuntimeError("Automatic download supports Windows, Linux and macOS only.")
    url = GOOGLE_PLATFORM_TOOLS[key]
    dest = dest or user_tools_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "platform-tools.zip"
        with urllib.request.urlopen(url, timeout=60) as resp, open(zip_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                target = (Path(tmp) / member).resolve()
                if not target.is_relative_to(Path(tmp).resolve()):
                    raise RuntimeError(f"Unsafe path in archive: {member}")
            zf.extractall(tmp)
        src = Path(tmp) / "platform-tools"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    if os.name != "nt":
        for name in ("adb", "fastboot"):
            p = dest / name
            if p.exists():
                p.chmod(0o755)
    return dest
