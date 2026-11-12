"""Small per-user app settings (a JSON file next to the profiles folder)."""

from __future__ import annotations

import json
from pathlib import Path


def path() -> Path:
    from opx.tools import user_tools_dir

    return user_tools_dir().parent / "settings.json"


def load() -> dict:
    try:
        data = json.loads(path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default=None):
    return load().get(key, default)


def put(key: str, value) -> None:
    data = load()
    data[key] = value
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(p)
