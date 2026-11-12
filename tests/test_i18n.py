import json
import subprocess
import sys
from pathlib import Path

import pytest

from opx import i18n

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def locales(tmp_path, monkeypatch):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    monkeypatch.setattr(i18n, "BUILTIN_DIR", builtin)
    monkeypatch.setattr(i18n, "user_locales_dir", lambda: user)
    yield builtin, user
    i18n.load("en")


def write(d, code, strings, name="X"):
    (d / f"{code}.json").write_text(json.dumps({"language": name, "strings": strings}), "utf-8")


def test_translate_and_fallback(locales):
    builtin, _ = locales
    write(builtin, "de", {"Backup": "Sicherung", "Reboot": ""}, "Deutsch")
    assert i18n.load("de_DE") == "de"
    assert i18n.tr("Backup") == "Sicherung"
    assert i18n.tr("Reboot") == "Reboot"  # empty = untranslated
    assert i18n.tr("Unknown text") == "Unknown text"
    assert i18n.available() == {"en": "English", "de": "Deutsch"}


def test_region_and_user_override(locales):
    builtin, user = locales
    write(builtin, "pt", {"Backup": "Cópia"})
    write(builtin, "pt_BR", {"Backup": "Backup BR"})
    write(user, "pt_BR", {"Backup": "Meu backup"})
    assert i18n.load("pt-BR") == "pt_BR" and i18n.tr("Backup") == "Meu backup"
    assert i18n.load("pt_PT") == "pt" and i18n.tr("Backup") == "Cópia"


def test_unknown_or_broken_falls_back_to_english(locales, monkeypatch):
    builtin, _ = locales
    (builtin / "fr.json").write_text("{not json", "utf-8")
    assert i18n.load("fr") == "en" and i18n.tr("Backup") == "Backup"
    monkeypatch.setenv("OPX_LANG", "xx_YY.UTF-8")
    assert i18n.system_language() == "xx_YY"


def test_template_is_up_to_date():
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract_strings.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout
    data = json.loads((ROOT / "src" / "opx" / "locales" / "template.json").read_text("utf-8"))
    assert "Phone not showing up?" in data["strings"]
