"""The logo files the app and installers depend on are present and well formed."""

import os
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "opx" / "gui" / "assets"
BRAND = ROOT / "assets" / "brand"
SIZES = (16, 24, 32, 48, 64, 128, 256)
ACCENT = "#D6322C"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize("size", SIZES)
def test_packaged_png_sizes(size):
    assert png_size(PKG / f"icon-{size}.png") == (size, size)


def test_large_png():
    assert png_size(BRAND / "opxflasher-512.png") == (512, 512)
    assert png_size(BRAND / "social-preview.png") == (1280, 640)


def test_ico_has_every_size_as_png_frames():
    data = (BRAND / "opxflasher.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    seen = set()
    for i in range(count):
        w, h, _c, _r, _planes, _bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i: 22 + 16 * i])
        frame = data[offset:offset + length]
        assert frame[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", frame[16:24]) == (w or 256, h or 256)
        seen.add((w or 256, h or 256))
    assert seen == {(s, s) for s in SIZES}


@pytest.mark.parametrize("path", [
    PKG / "icon.svg",
    PKG / "icon-small.svg",
    BRAND / "wordmark-on-dark.svg",
    BRAND / "wordmark-on-light.svg",
])
def test_svgs_parse_and_use_the_accent(path):
    root = ET.parse(path).getroot()
    assert root.tag.endswith("svg")
    assert root.get("viewBox")
    assert ACCENT in path.read_text(encoding="utf-8")


def test_wordmarks_have_no_live_text():
    # Text is outlined so it renders the same without Space Grotesk installed.
    for name in ("wordmark-on-dark.svg", "wordmark-on-light.svg"):
        assert "<text" not in (BRAND / name).read_text(encoding="utf-8")


def test_app_icon_loads_every_size():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from opx.gui.icon import SIZES as ICON_SIZES
    from opx.gui.icon import app_icon

    QApplication.instance() or QApplication([])
    icon = app_icon()
    assert not icon.isNull()
    got = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert got == {(s, s) for s in ICON_SIZES}
