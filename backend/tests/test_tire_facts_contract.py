"""docs/tire_facts_contract.md must match the API schema (Phase 6F acceptance): every fact's
source path exists in TirePrediction, and every TirePrediction field is either a source or
explicitly listed as not exposed. Field names are diffed in both directions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.export_openapi import openapi_document

DOC = Path(__file__).resolve().parents[2] / "docs" / "tire_facts_contract.md"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)$")


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start: nxt if nxt != -1 else len(text)]


def facts(text: str) -> dict[str, str]:
    """fact ID → source path, from the Facts table."""
    out = {}
    for line in section(text, "## Facts").splitlines():
        m = ROW.match(line)
        if m:
            source = re.search(r"`([^`]+)`", m.group(2))
            assert source, f"fact {m.group(1)} has no source path"
            out[m.group(1)] = source.group(1)
    return out


def not_exposed(text: str) -> set[str]:
    return {m.group(1) for line in section(text, "## Not exposed as facts").splitlines() if (m := ROW.match(line))}


def resolve(schemas: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while True:
        if "$ref" in node:
            node = schemas[node["$ref"].rsplit("/", 1)[1]]
        elif "anyOf" in node:  # Optional[X] → X
            node = next(n for n in node["anyOf"] if n.get("type") != "null")
        else:
            return node


def lookup(schemas: dict[str, Any], path: str) -> dict[str, Any]:
    node = schemas["TirePrediction"]
    for part in path.split("."):
        name, index = re.fullmatch(r"(\w+)(\[\d+\])?", part).groups()  # type: ignore[union-attr]
        props = resolve(schemas, node).get("properties", {})
        assert name in props, f"{path}: no field {name!r}"
        node = props[name]
        if index:
            node = resolve(schemas, node)
            assert node.get("type") == "array", f"{path}: {name} is not an array"
            node = node["items"]
    return resolve(schemas, node)


def leaf_paths(schemas: dict[str, Any]) -> set[str]:
    """Top-level fields plus the fields of the nested pit_window and model objects."""
    top = resolve(schemas, schemas["TirePrediction"])["properties"]
    out = set(top)
    for nested in ("pit_window", "model"):
        out |= {f"{nested}.{f}" for f in resolve(schemas, top[nested])["properties"]}
    return out


def test_every_fact_source_exists_in_the_api_schema():
    schemas = openapi_document()["components"]["schemas"]
    text = DOC.read_text(encoding="utf-8")
    table = facts(text)
    assert len(table) >= 30 and all(k.startswith("tire.") for k in table)
    for fact, source in table.items():
        lookup(schemas, source)  # raises with the offending path
    for field in not_exposed(text):
        lookup(schemas, field)


def test_every_api_field_is_a_fact_or_explicitly_not_exposed():
    schemas = openapi_document()["components"]["schemas"]
    text = DOC.read_text(encoding="utf-8")
    covered = {re.sub(r"\[\d+\].*$", "", s) for s in facts(text).values()} | not_exposed(text)
    covered |= {p.split(".")[0] for p in covered}  # a nested source covers its parent object
    missing = sorted(leaf_paths(schemas) - covered)
    assert not missing, f"API fields missing from docs/tire_facts_contract.md: {missing}"


def test_required_plan_facts_are_present():
    table = facts(DOC.read_text(encoding="utf-8"))
    for fact in ("tire.compound", "tire.tyre_age_laps", "tire.clean_laps_used", "tire.deg_s_per_lap", "tire.deg_ci_low",
                 "tire.deg_ci_high", "tire.window_earliest_lap", "tire.window_central_lap", "tire.window_latest_lap",
                 "tire.threshold_s", "tire.confidence", "tire.model_version"):
        assert fact in table, fact


def test_the_diff_catches_drift():
    schemas = openapi_document()["components"]["schemas"]
    text = DOC.read_text(encoding="utf-8")
    dropped = "\n".join(line for line in text.splitlines() if "`remaining_competitive_laps`" not in line)
    covered = {re.sub(r"\[\d+\].*$", "", s) for s in facts(dropped).values()} | not_exposed(dropped)
    covered |= {p.split(".")[0] for p in covered}
    assert "remaining_competitive_laps" in leaf_paths(schemas) - covered
    try:
        lookup(schemas, "pit_window.no_such_field")
    except AssertionError as exc:
        assert "no_such_field" in str(exc)
    else:
        raise AssertionError("a bad source path was accepted")
