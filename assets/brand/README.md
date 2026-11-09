# OPxFlasher brand

The icon is a chip with a tick: firmware that has been checked before it is
written.

| File | Use |
| --- | --- |
| `../../src/opx/gui/assets/icon.svg` | Master icon, 32 px and up |
| `../../src/opx/gui/assets/icon-small.svg` | 16–32 px: two pins per side, heavier strokes |
| `../../src/opx/gui/assets/icon-*.png` | Window and taskbar icon, packaged with the app |
| `opxflasher.ico` | Windows (.exe and shortcuts), 16–256 px |
| `opxflasher-512.png` | Linux desktop file and AppImage |
| `wordmark-on-dark.svg`, `wordmark-on-light.svg` | Icon + name lockup |
| `social-preview.png` | GitHub social preview (Settings → General → Social preview) |

Only the two SVG icons are drawn by hand. Everything else is generated:

```sh
python scripts/build_brand.py                                  # icons
python scripts/build_brand.py --font "SpaceGrotesk[wght].ttf"  # icons + wordmarks
```

## Colours

| Role | Hex |
| --- | --- |
| Accent (tick, "OPx") | `#D6322C` |
| Icon tile | `#1C1F26` |
| Icon lines | `#F5F6F8` |
| Text on dark | `#E6E8EE` |
| Text on light / page background | `#15171C` |
| Muted text | `#9AA3B5` |

## Type

The name is set in Space Grotesk Bold (SIL OFL 1.1), tracking −0.01 em, with
"OPx" in the accent colour. In the wordmark files the text is outlined, so they
look the same everywhere.

## Don't

- Recolour the tick to anything other than the accent, or put the icon on a
  busy photo.
- Use the large icon below 32 px; use the small drawing instead.
- Use the logo in a way that suggests OPxFlasher is made or endorsed by OPPO
  or OnePlus. It isn't.
