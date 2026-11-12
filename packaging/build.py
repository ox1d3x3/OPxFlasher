"""Build the portable, one-file OPxFlasher apps.

    python packaging/build.py            # on Windows -> dist/OPxFlasher-<ver>-windows-x64.exe
                                         # on Linux   -> dist/OPxFlasher-<ver>-linux-x86_64.AppImage
    python packaging/build.py --smoke    # also start the built app once with --smoke-test

Needs: pip install ".[gui,zstd]" pyinstaller. The Linux build also downloads
appimagetool from github.com/AppImage/appimagetool unless APPIMAGETOOL points
to one. Neither build bundles Google's platform-tools: users get them with
"Download platform-tools" in the app (or `opx tools fetch`), or put a
platform-tools folder next to the .exe / .AppImage.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)

# Qt pieces the app doesn't use; leaving them out keeps the download smaller.
EXCLUDES = [
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
    "PySide6.QtOpenGL", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus",
    "PySide6.QtConcurrent", "PySide6.QtXml", "PySide6.QtHelp",
    "tkinter", "unittest", "pydoc",
]


BUILD_INFO = ROOT / "src" / "opx" / "_build.py"


def version() -> str:
    """OPX_VERSION_LABEL (set by the release workflow, e.g. 0.7.0-rc.3) or the package version.
    The label is written into the app so its title shows the exact build."""
    label = os.environ.get("OPX_VERSION_LABEL", "").strip()
    if label:
        BUILD_INFO.write_text(f'VERSION = "{label}"\n', encoding="utf-8")
        return label
    BUILD_INFO.unlink(missing_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    from opx import __version__

    return __version__


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyinstaller(onefile: bool, icon: Path) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "OPxFlasher", "--windowed",
        "--onefile" if onefile else "--onedir",
        "--icon", icon,
        "--paths", ROOT / "src",
        "--collect-data", "opx",
        "--distpath", WORK / "dist", "--workpath", WORK / "work", "--specpath", WORK,
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])))
    run(cmd + [ROOT / "packaging" / "launcher.py"], env=env)
    return WORK / "dist" / ("OPxFlasher.exe" if onefile else "OPxFlasher")


def build_windows(ver: str) -> Path:
    exe = pyinstaller(True, ROOT / "assets" / "brand" / "opxflasher.ico")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"OPxFlasher-{ver}-windows-x64.exe"
    shutil.copy2(exe, out)
    return out


def appimagetool() -> Path:
    if os.environ.get("APPIMAGETOOL"):
        return Path(os.environ["APPIMAGETOOL"])
    tool = WORK / "appimagetool-x86_64.AppImage"
    if not tool.exists():
        tool.parent.mkdir(parents=True, exist_ok=True)
        print("downloading appimagetool", flush=True)
        urllib.request.urlretrieve(APPIMAGETOOL_URL, tool)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


def build_linux(ver: str) -> Path:
    bundle = pyinstaller(False, ROOT / "assets" / "brand" / "opxflasher-512.png")
    appdir = WORK / "OPxFlasher.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(bundle, appdir / "usr" / "lib" / "opxflasher")
    shutil.copy2(ROOT / "assets" / "brand" / "opxflasher-512.png", appdir / "opxflasher.png")
    icons = appdir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "brand" / "opxflasher-512.png", icons / "opxflasher.png")
    shutil.copy2(ROOT / "packaging" / "linux" / "opxflasher.desktop", appdir / "opxflasher.desktop")
    apprun = appdir / "AppRun"
    apprun.write_text(
        '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
        'exec "$HERE/usr/lib/opxflasher/OPxFlasher" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(0o755)

    DIST.mkdir(exist_ok=True)
    out = DIST / f"OPxFlasher-{ver}-linux-x86_64.AppImage"
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    run([appimagetool(), "--no-appstream", appdir, out], env=env)
    return out


def smoke(app: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPIMAGE_EXTRACT_AND_RUN="1")
    proc = subprocess.run([str(app), "--smoke-test"], env=env, capture_output=True,
                          text=True, timeout=300)
    print(proc.stdout, proc.stderr, sep="\n")
    if proc.returncode != 0:
        sys.exit(f"smoke test failed with exit code {proc.returncode}")
    print("smoke test passed", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the portable OPxFlasher app")
    ap.add_argument("--smoke", action="store_true", help="run the built app once with --smoke-test")
    args = ap.parse_args()
    ver = version()
    if sys.platform.startswith("win"):
        out = build_windows(ver)
    elif sys.platform.startswith("linux"):
        out = build_linux(ver)
    else:
        sys.exit("Portable builds are made on Windows (.exe) and Linux (.AppImage). "
                 "On macOS, install with pipx; see the README.")
    print(f"built {out} ({out.stat().st_size / 1e6:.1f} MB)", flush=True)
    if args.smoke:
        smoke(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
