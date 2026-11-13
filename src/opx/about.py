"""Who makes OPxFlasher, where it lives, and what this build is.

Shown in Help > About (app) and by ``opx about`` (command line), and used for
the "Copy details" text in bug reports.
"""

from __future__ import annotations

import platform
import sys

from opx import __version__

APP = "OPxFlasher"
TAGLINE = "AIO tool for OnePlus / OPPO devices"
DEVELOPER = "Mahabub Subhany"
HANDLE = "Ox1d3x3"
DEVELOPER_URL = "https://github.com/ox1d3x3"
REPO_URL = "https://github.com/ox1d3x3/OPxFlasher"
RELEASES_URL = REPO_URL + "/releases"
ISSUES_URL = REPO_URL + "/issues/new/choose"
GUIDE_URL = REPO_URL + "/blob/main/docs/USER_GUIDE.md"
CLI_URL = REPO_URL + "/blob/main/docs/CLI.md"
CREDITS_URL = REPO_URL + "/blob/main/CREDITS.md"
LICENSE_NAME = "GNU GPL v3.0 or later"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
SUPPORT = (("Ko-fi", "https://ko-fi.com/mahabubsubhany"), ("PayPal", "https://paypal.me/mason7gg"))
COPYRIGHT = f"Copyright (C) 2026 {DEVELOPER}"


def shown(text: str) -> str:
    """Text for the screen: stops fonts drawing the x in 'Ox1d3x3' as a multiplication sign
    (some fonts swap 'x' between digits for '×'). Never use it in links."""
    import re

    return re.sub(r"(?<=\d)x(?=\d)", "\u200cx\u200c", text)


def build_kind(version: str = __version__) -> str:
    if "-rc." in version:
        return "release candidate"
    if version.count(".") == 2 and version.replace(".", "").isdigit():
        return "release"
    return "development build"


def details(qt_version: str = "") -> dict[str, str]:
    """Plain facts about this copy, for About and bug reports."""
    out = {
        "Version": f"{__version__} ({build_kind()})",
        "Developer": f"{DEVELOPER} ({HANDLE}), {DEVELOPER_URL}",
        "Project": REPO_URL,
        "Licence": LICENSE_NAME,
        "Python": platform.python_version(),
        "System": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "Packaged": "portable app" if getattr(sys, "frozen", False) else "Python install",
    }
    if qt_version:
        out["Qt"] = qt_version
    return out


def text(qt_version: str = "") -> str:
    lines = [f"{APP} - {TAGLINE}"]
    lines += [f"{k}: {v}" for k, v in details(qt_version).items()]
    return "\n".join(lines)
