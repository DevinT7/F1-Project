"""Phase 1 — only app/ingestion/fastf1_client.py may import fastf1.

Parses every backend source file (AST, so comments/strings don't count) and fails on
any `import fastf1` / `from fastf1 ...` outside the adapter. ml/ is P3's standalone
offline pipeline with its own requirements and is out of scope.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ALLOWED = {Path("app/ingestion/fastf1_client.py")}


def _imports_fastf1(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "fastf1" for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "fastf1":
            lines.append(node.lineno)
    return lines


def test_only_the_adapter_imports_fastf1():
    offenders = {}
    for folder in ("app", "scripts"):
        for path in (BACKEND / folder).rglob("*.py"):
            rel = path.relative_to(BACKEND)
            if rel not in ALLOWED and (hits := _imports_fastf1(path)):
                offenders[str(rel)] = hits
    assert offenders == {}, f"fastf1 imported outside the adapter: {offenders}"


def test_the_adapter_really_is_the_import_site():
    assert _imports_fastf1(BACKEND / "app/ingestion/fastf1_client.py")
