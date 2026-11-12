"""Collect every tr("...") text into src/opx/locales/template.json, and report
what each existing translation is missing.

    python scripts/extract_strings.py           # update the template, print coverage
    python scripts/extract_strings.py --check   # exit 1 if the template is out of date
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "opx"
LOCALES = SRC / "locales"


def collect(root: Path = SRC) -> list[str]:
    found = set()
    for py in root.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text("utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
    return sorted(found)


def template(strings: list[str]) -> str:
    return json.dumps({"language": "", "strings": {s: "" for s in strings}}, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    strings = collect()
    text = template(strings)
    path = LOCALES / "template.json"
    if "--check" in argv:
        current = path.read_text("utf-8") if path.exists() else ""
        if current != text:
            print("locales/template.json is out of date: run python scripts/extract_strings.py")
            return 1
        return 0
    path.write_text(text, "utf-8")
    print(f"{len(strings)} texts -> {path}")
    for f in sorted(LOCALES.glob("*.json")):
        if f.stem == "template":
            continue
        have = json.loads(f.read_text("utf-8")).get("strings", {})
        done = sum(1 for s in strings if have.get(s, "").strip())
        print(f"  {f.stem}: {done}/{len(strings)} translated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
