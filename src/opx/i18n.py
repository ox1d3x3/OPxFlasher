"""App text translation.

Text in the code stays English and goes through ``tr("...")``. A translation is
a JSON file in ``opx/locales/`` (or the user's ``locales`` folder next to their
profiles) named after the language, e.g. ``de.json`` or ``pt_BR.json``:

    {"language": "Deutsch", "strings": {"Backup": "Sicherung", ...}}

Missing entries fall back to English, so a partial translation is fine.
The language comes from OPX_LANG, else the app's Language setting, else the
system locale. ``{name}`` fields
are kept: ``tr("Saved {n} files").format(n=3)``.
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "locales"
_strings: dict[str, str] = {}
_lang = "en"


def user_locales_dir() -> Path:
    from opx.profile import user_profiles_dir

    return user_profiles_dir().parent / "locales"


def _dirs() -> list[Path]:
    return [BUILTIN_DIR, user_locales_dir()]


def available() -> dict[str, str]:
    """Language code -> its own name, English first."""
    out = {"en": "English"}
    for d in _dirs():
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            if f.stem == "template":
                continue
            try:
                out[f.stem] = json.loads(f.read_text("utf-8")).get("language") or f.stem
            except (OSError, ValueError):
                continue
    return out


def system_language() -> str:
    """OPX_LANG, else the language picked in the app, else the system's."""
    code = os.environ.get("OPX_LANG", "").strip()
    if not code:
        from opx import settings

        code = str(settings.get("language") or "").strip()
    if not code:
        try:
            code = locale.getlocale()[0] or ""
        except ValueError:
            code = ""
    return code.split(".")[0] or "en"


def load(code: str | None = None) -> str:
    """Switch language; tries 'pt_BR' then 'pt'. Returns the language used."""
    global _strings, _lang
    code = (code or system_language()).replace("-", "_")
    for cand in (code, code.split("_")[0]):
        for d in reversed(_dirs()):  # a user file overrides a built-in one
            f = d / f"{cand}.json"
            try:
                data = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            strings = data.get("strings", {})
            _strings = {k: v for k, v in strings.items() if isinstance(v, str) and v.strip()}
            _lang = cand
            return _lang
    _strings, _lang = {}, "en"
    return _lang


def language() -> str:
    return _lang


def tr(text: str) -> str:
    return _strings.get(text, text)
