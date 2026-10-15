"""Generate docs/api.md from the reviewed OpenAPI snapshot (backend/openapi.json).

    cd backend && python -m scripts.gen_api_docs            # write docs/api.md
    cd backend && python -m scripts.gen_api_docs --check    # CI: fail when docs/api.md is stale

One section per tag, one entry per operation: method + path, summary, access (public / signed in /
admin), parameters, request body, responses. The interactive reference is /docs on a running API.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
SPEC = BACKEND / "openapi.json"
OUT = BACKEND.parent / "docs" / "api.md"
ADMIN_HINTS = ("/ingest", "/admin/", "/jobs/")


def _ref(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "—"
    if "$ref" in schema:
        return f"`{schema['$ref'].rsplit('/', 1)[-1]}`"
    if schema.get("type") == "array":
        return f"array of {_ref(schema.get('items'))}"
    return f"`{schema.get('type', 'object')}`"


def _access(path: str, op: dict[str, Any]) -> str:
    if any(h in path for h in ADMIN_HINTS):
        return "admin"
    if op.get("security"):
        return "signed in"
    return "public"


def render(spec: dict[str, Any]) -> str:
    lines = [
        "# RaceIQ API reference",
        "",
        f"Generated from `backend/openapi.json` (API version {spec['info'].get('version')}) by "
        "`python -m scripts.gen_api_docs`; do not edit by hand. Interactive docs: `/docs` on a running API.",
        "",
        "Every error uses one envelope: `{\"error\": {\"code\", \"message\", \"status_code\", \"request_id\"}}`. "
        "Send `X-Request-ID` to correlate logs; the response echoes it. Access: **public**, **signed in** "
        "(`Authorization: Bearer <access token>`), or **admin**.",
        "",
    ]
    tags = [t["name"] for t in spec.get("tags", [])]
    ops: dict[str, list[tuple[str, str, dict[str, Any]]]] = {t: [] for t in tags}
    for path, item in spec["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for method, op in item.items():
            tag = (op.get("tags") or ["other"])[0]
            ops.setdefault(tag, []).append((method.upper(), path, op))
    descriptions = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}
    for tag in [*tags, *(t for t in ops if t not in tags)]:
        entries = ops.get(tag) or []
        if not entries:
            continue
        lines += [f"## {tag.removeprefix('v1: ').capitalize()}", "", descriptions.get(tag, ""), ""]
        for method, path, op in sorted(entries, key=lambda e: (e[1], e[0])):
            lines += [f"### `{method} {path}`", "", f"**{op.get('summary', '')}** · access: {_access(path, op)}", ""]
            if op.get("description"):
                lines += [op["description"].strip(), ""]
            params = op.get("parameters") or []
            if params:
                lines += ["| Parameter | In | Required | Type | Description |", "|---|---|---|---|---|"]
                for p in params:
                    desc = (p.get("description") or p.get("schema", {}).get("description") or "").replace("|", "\\|")
                    lines.append(f"| `{p['name']}` | {p['in']} | {'yes' if p.get('required') else 'no'} | "
                                 f"{_ref(p.get('schema'))} | {desc} |")
                lines.append("")
            body = (op.get("requestBody") or {}).get("content", {}).get("application/json")
            if body:
                lines += [f"Request body: {_ref(body.get('schema'))}", ""]
            responses = op.get("responses") or {}
            parts = []
            for code, resp in sorted(responses.items()):
                schema = (resp.get("content") or {}).get("application/json", {}).get("schema")
                parts.append(f"`{code}` {resp.get('description', '')}" + (f" ({_ref(schema)})" if schema else ""))
            lines += ["Responses: " + "; ".join(parts), ""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.gen_api_docs")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render(json.loads(SPEC.read_text(encoding="utf-8")))
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current.replace("\r\n", "\n") != text:
            print("docs/api.md is out of date: run `python -m scripts.gen_api_docs`", file=sys.stderr)
            return 1
        print("docs/api.md is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
