"""Regenerate the OPxFlasher brand assets from the two hand-drawn SVG sources.

The sources are ``src/opx/gui/assets/icon.svg`` (32 px and up) and
``icon-small.svg`` (16 to 32 px, fewer pins and heavier strokes so it stays
legible). Everything else is generated from them:

    src/opx/gui/assets/icon-<size>.png   window / taskbar icon, packaged with opx
    assets/brand/opxflasher.ico          Windows icon (16-256 px, each size drawn, not scaled)
    assets/brand/opxflasher-512.png      Linux / AppImage icon
    assets/brand/wordmark-on-dark.svg    icon + "OPxFlasher" lockup, text as outlines
    assets/brand/wordmark-on-light.svg
    assets/brand/social-preview.png      1280x640 GitHub social preview

Run from the repo root:

    python scripts/build_brand.py                                # icons only
    python scripts/build_brand.py --font "SpaceGrotesk[wght].ttf"  # icons + wordmarks

Needs PySide6 and Pillow. The wordmark step also needs fontTools and uharfbuzz,
plus the Space Grotesk variable font (SIL OFL 1.1, from github.com/google/fonts).
The text is converted to outlines, so the font itself is not shipped.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_ASSETS = ROOT / "src" / "opx" / "gui" / "assets"
BRAND = ROOT / "assets" / "brand"

ICON_SVG = PKG_ASSETS / "icon.svg"
SMALL_SVG = PKG_ASSETS / "icon-small.svg"

PACKAGE_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICO_SIZES = PACKAGE_SIZES
SMALL_MAX = 32  # sizes up to this use icon-small.svg

ACCENT = "#D6322C"
TEXT_ON_DARK = "#E6E8EE"
TEXT_ON_LIGHT = "#15171C"
MUTED = "#9AA3B5"
BG = "#15171C"

WORDMARK = "OPxFlasher"
ACCENT_CHARS = 3  # "OPx"
TAGLINE = "AIO tool for OnePlus / OPPO devices"


# --------------------------------------------------------------------------- render


def _qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([])
    return app, QByteArray, QRectF, Qt, QImage, QPainter, QSvgRenderer


def render_png(svg: bytes, width: int, height: int | None = None) -> bytes:
    """Render SVG markup to PNG bytes at an exact pixel size."""
    _app, QByteArray, QRectF, Qt, QImage, QPainter, QSvgRenderer = _qt()
    from PySide6.QtCore import QBuffer, QIODevice

    height = height or width
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise SystemExit("could not parse SVG")
    img = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(p, QRectF(0, 0, width, height))
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def icon_png(size: int) -> bytes:
    src = SMALL_SVG if size <= SMALL_MAX else ICON_SVG
    return render_png(src.read_bytes(), size)


def build_icons() -> None:
    from PIL import Image

    PKG_ASSETS.mkdir(parents=True, exist_ok=True)
    BRAND.mkdir(parents=True, exist_ok=True)

    frames = {}
    for size in PACKAGE_SIZES:
        data = icon_png(size)
        (PKG_ASSETS / f"icon-{size}.png").write_bytes(data)
        frames[size] = Image.open(io.BytesIO(data)).convert("RGBA")
        print(f"  icon-{size}.png")

    (BRAND / "opxflasher-512.png").write_bytes(icon_png(512))
    print("  opxflasher-512.png")

    # Pillow picks the appended frame that matches each requested size, so
    # every ICO entry is the hand-tuned render rather than a downscale.
    largest = frames[max(ICO_SIZES)]
    others = [frames[s] for s in ICO_SIZES if s != max(ICO_SIZES)]
    largest.save(
        BRAND / "opxflasher.ico",
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=others,
    )
    print("  opxflasher.ico")


# --------------------------------------------------------------------------- wordmark


def _num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


class Outliner:
    """Shapes text with HarfBuzz and returns it as SVG path data."""

    def __init__(self, font_path: Path, weight: int):
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont

        font = TTFont(font_path)
        if "fvar" in font:
            font = instantiateVariableFont(font, {"wght": weight})
        buf = io.BytesIO()
        font.save(buf)
        self.hb = hb
        self.face = hb.Face(buf.getvalue())
        self.hbfont = hb.Font(self.face)
        self.font = font
        self.glyphs = font.getGlyphSet()
        self.order = font.getGlyphOrder()
        self.upem = self.face.upem
        self.cap_height = font["OS/2"].sCapHeight

    def shape(self, text: str):
        b = self.hb.Buffer()
        b.add_str(text)
        b.guess_segment_properties()
        self.hb.shape(self.hbfont, b, {"kern": True, "liga": True})
        return list(zip(b.glyph_infos, b.glyph_positions))

    def width(self, text: str, size: float, tracking_em: float = 0.0) -> float:
        runs = self.shape(text)
        track = tracking_em * self.upem
        total = sum(pos.x_advance for _, pos in runs) + track * (len(runs) - 1)
        return total * size / self.upem

    def paths(self, text: str, size: float, x0: float, baseline: float,
              tracking_em: float = 0.0) -> list[tuple[int, str]]:
        """Return (cluster, path data) per glyph, in user units."""
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen

        scale = size / self.upem
        track = tracking_em * self.upem
        out = []
        x = 0.0
        for info, pos in self.shape(text):
            name = self.order[info.codepoint]
            pen = SVGPathPen(self.glyphs, ntos=_num)
            t = (scale, 0, 0, -scale,
                 x0 + (x + pos.x_offset) * scale, baseline - pos.y_offset * scale)
            self.glyphs[name].draw(TransformPen(pen, t))
            d = pen.getCommands()
            if d:
                out.append((info.cluster, d))
            x += pos.x_advance + track
        return out


def icon_inner() -> str:
    """The icon's drawing elements without the outer <svg> and <title>."""
    svg = ICON_SVG.read_text(encoding="utf-8")
    inner = svg[svg.index(">") + 1: svg.rindex("</svg>")]
    inner = re.sub(r"\s*<title[^>]*>.*?</title>", "", inner, flags=re.DOTALL)
    return inner.strip()


# Lockup proportions follow the design canvas: 48 px icon, 12 px gap, 30 px
# bold text, tracking -0.01 em. In icon units (120) that is a 30 gap and 75 text.
LOCKUP_H = 120.0
GAP = 30.0
TEXT_SIZE = 75.0
TRACKING = -0.01


def lockup_body(bold: Outliner, text_color: str) -> tuple[str, float]:
    baseline = LOCKUP_H / 2 + bold.cap_height * TEXT_SIZE / bold.upem / 2
    x0 = LOCKUP_H + GAP
    accent, rest = [], []
    for cluster, d in bold.paths(WORDMARK, TEXT_SIZE, x0, baseline, TRACKING):
        (accent if cluster < ACCENT_CHARS else rest).append(d)
    width = x0 + bold.width(WORDMARK, TEXT_SIZE, TRACKING)
    body = (
        f"<g>{icon_inner()}</g>\n"
        f'<path fill="{ACCENT}" d="{" ".join(accent)}"/>\n'
        f'<path fill="{text_color}" d="{" ".join(rest)}"/>'
    )
    return body, width


def build_wordmarks(font_path: Path) -> None:
    bold = Outliner(font_path, 700)
    medium = Outliner(font_path, 500)

    for name, color in (("on-dark", TEXT_ON_DARK), ("on-light", TEXT_ON_LIGHT)):
        body, width = lockup_body(bold, color)
        w = _num(width + 2)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {_num(LOCKUP_H)}" '
            f'width="{w}" height="{_num(LOCKUP_H)}" role="img" aria-labelledby="t">\n'
            f'<title id="t">OPxFlasher</title>\n{body}\n</svg>\n'
        )
        (BRAND / f"wordmark-{name}.svg").write_text(svg, encoding="utf-8")
        print(f"  wordmark-{name}.svg")

    # Social preview: 1280x640, lockup centred, tagline underneath.
    W, H = 1280, 640
    body, width = lockup_body(bold, TEXT_ON_DARK)
    scale = 720 / width
    lock_h = LOCKUP_H * scale
    tag_size = 34.0
    gap = 56.0
    block_h = lock_h + gap + tag_size
    top = (H - block_h) / 2
    lx = (W - width * scale) / 2
    tag_w = medium.width(TAGLINE, tag_size)
    tag_base = top + lock_h + gap + medium.cap_height * tag_size / medium.upem
    tag = " ".join(d for _, d in medium.paths(TAGLINE, tag_size, (W - tag_w) / 2, tag_base))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">\n'
        f'<rect width="{W}" height="{H}" fill="{BG}"/>\n'
        f'<g transform="translate({_num(lx)} {_num(top)}) scale({scale:.5f})">\n{body}\n</g>\n'
        f'<path fill="{MUTED}" d="{tag}"/>\n</svg>\n'
    )
    (BRAND / "social-preview.png").write_bytes(render_png(svg.encode(), W, H))
    print("  social-preview.png")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", type=Path,
                    help="Space Grotesk variable font; also rebuilds the wordmarks")
    args = ap.parse_args(argv)
    print("icons:")
    build_icons()
    if args.font:
        print("wordmarks:")
        build_wordmarks(args.font)
    return 0


if __name__ == "__main__":
    sys.exit(main())
