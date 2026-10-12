"""Export the OpenAPI document to backend/openapi.json (the reviewed snapshot).

    cd backend && python -m scripts.export_openapi            # write openapi.json
    cd backend && python -m scripts.export_openapi --check    # exit 1 if the app drifted (CI)

frontend `npm run gen-types` turns openapi.json into src/api/types.ts.
info.version is pinned to "snapshot" so version bumps don't churn the file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.main import app

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def openapi_document() -> dict[str, Any]:
    doc = json.loads(json.dumps(app.openapi()))  # deep copy, plain JSON types
    doc["info"]["version"] = "snapshot"
    return doc


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if openapi.json is out of date")
    args = parser.parse_args()
    text = render(openapi_document())
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("openapi.json is out of date: run `python -m scripts.export_openapi` and review the diff",
                  file=sys.stderr)
            return 1
        print("openapi.json is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
