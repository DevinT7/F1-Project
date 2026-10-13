"""Phase 7A — the FactSheet maps EVERY value of its sources. Written before the builder.

Every leaf of the findings JSON (Phase 3) and the tyre JSON (Phase 6E, per
docs/tire_facts_contract.md) must be accounted for:
  * a number → exactly one numeric fact (fact.source is its JSON pointer)
  * a null   → exactly one fact, numeric or categorical (a null is a fact: "unavailable")
  * a string / bool / list of strings → a categorical fact
  * or an explicitly documented exclusion (FINDINGS_EXCLUDED / the contract's "Not exposed").
An unmapped leaf fails, so a new field in either source cannot be silently forgotten.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.ai.factsheet import FINDINGS_EXCLUDED, TIRE_NOT_EXPOSED, build_factsheet, factsheet_from_inputs
from app.ai.numbers import display_forms

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
NAMES = sorted(p.name.removesuffix(".inputs.json") for p in FIXTURES.glob("*.inputs.json"))


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.inputs.json").read_text(encoding="utf-8"))


def leaves(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """(JSON pointer, value) for every leaf. A list of scalars that are not numbers is one leaf."""
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in leaves(v, f"{path}/{k}")]
    if isinstance(obj, list):
        if obj and all(isinstance(v, str) for v in obj):
            return [(path, obj)]
        if not obj:
            return [(path, obj)]
        return [x for i, v in enumerate(obj) for x in leaves(v, f"{path}/{i}")]
    return [(path, obj)]


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(re.fullmatch(p, path) for p in patterns)


@pytest.mark.parametrize("name", NAMES)
def test_every_source_leaf_maps_to_exactly_one_fact(name):
    inputs = load(name)
    fs = factsheet_from_inputs(inputs)
    numeric = Counter(f.source for f in fs.facts)
    categorical = Counter(c.source for c in fs.categorical_facts)
    sources = {"findings": inputs["findings"], "tire": inputs.get("tire")}
    unmapped, duplicated = [], []
    for root, patterns in (("findings", FINDINGS_EXCLUDED), ("tire", TIRE_NOT_EXPOSED)):
        if sources[root] is None:
            continue
        for path, value in leaves(sources[root]):
            key = f"{root}:{path}"
            if excluded(path, patterns):
                assert numeric[key] == 0 and categorical[key] == 0, f"{key} is excluded but also mapped"
                continue
            n = numeric[key] + categorical[key]
            if is_number(value):
                if numeric[key] != 1 or categorical[key]:
                    (unmapped if numeric[key] == 0 else duplicated).append(key)
            elif n != 1:
                (unmapped if n == 0 else duplicated).append(key)
    assert not unmapped, f"source values with no fact: {unmapped[:20]}"
    assert not duplicated, f"source values mapped more than once: {duplicated[:20]}"


@pytest.mark.parametrize("name", NAMES)
def test_every_fact_points_at_a_real_source_value(name):
    inputs = load(name)
    fs = factsheet_from_inputs(inputs)
    for f in [*fs.facts, *fs.categorical_facts]:
        if f.source.startswith("derived:"):
            continue
        root, pointer = f.source.split(":", 1)
        node: Any = inputs["findings"] if root == "findings" else inputs["tire"]
        for part in pointer.strip("/").split("/"):
            node = node[int(part)] if isinstance(node, list) else node[part]
        if hasattr(f, "unit"):
            assert node == f.value or (node is None and f.value is None), f.id
        else:
            assert node == f.value, f.id


@pytest.mark.parametrize("name", NAMES)
def test_fact_ids_are_unique_and_well_formed(name):
    fs = factsheet_from_inputs(load(name))
    ids = [f.id for f in fs.facts] + [c.id for c in fs.categorical_facts]
    assert len(ids) == len(set(ids))
    bad = [i for i in ids if not re.fullmatch(r"([A-Z]+\d*|[a-z_]+)\.[a-z_0-9]+(\.[a-z_0-9]+)*", i)]
    assert not bad, bad[:10]


@pytest.mark.parametrize("name", NAMES)
def test_nulls_are_facts_and_never_render_as_zero(name):
    fs = factsheet_from_inputs(load(name))
    for f in fs.facts:
        if f.value is None:
            assert f.display == [] and f.meaning.startswith("unavailable"), f.id
        else:
            assert f.display and all(math.isfinite(float(d)) for d in f.display), f.id
            assert f.display == display_forms(f.value, f.unit)


@pytest.mark.parametrize("name", NAMES)
def test_hash_is_stable_and_sensitive(name):
    inputs = load(name)
    a, b = factsheet_from_inputs(inputs), factsheet_from_inputs(json.loads(json.dumps(inputs)))
    assert a.factsheet_hash == b.factsheet_hash and len(a.factsheet_hash) == 64
    poked = json.loads(json.dumps(inputs))
    poked["findings"]["lap_summary"]["time_loss_s"] += 0.001
    assert factsheet_from_inputs(poked).factsheet_hash != a.factsheet_hash


@pytest.mark.parametrize("name", NAMES)
def test_committed_factsheet_matches_the_builder(name):
    """The committed fixture factsheets are exactly what the builder produces (golden files)."""
    expected = json.loads((FIXTURES / f"{name}.factsheet.json").read_text(encoding="utf-8"))
    assert factsheet_from_inputs(load(name)).model_dump(mode="json") == expected


def test_fixture_set_covers_the_required_cases():
    flags = {name: factsheet_from_inputs(load(name)) for name in NAMES}
    assert len(flags) >= 5
    assert any(f.id.endswith(".braking_start_diff_m") and f.value is None for fs in flags.values() for f in fs.facts), \
        "a corner with no braking"
    assert any(fs.tire.get("status") == "unavailable" for fs in flags.values()), "tyre unavailable"
    assert any(fs.data_quality.low_confidence_segments for fs in flags.values()), "low-confidence segments"
    assert any(fs.session.session_type == "Q" and fs.tire.get("status") == "unavailable" for fs in flags.values()), \
        "qualifying with no tyre forecast"
    assert any(next((f.value for f in fs.facts if f.id == "tire.window_central_lap"), None) is not None
               for fs in flags.values()), "race with a pit window"


def test_build_factsheet_accepts_the_api_models_directly():
    inputs = load(NAMES[0])
    from app.ai.factsheet import FactSheetContext
    from app.processing.insights import FindingsReport

    fs = build_factsheet(FindingsReport.model_validate(inputs["findings"]), inputs["tire"],
                         FactSheetContext.model_validate(inputs["context"]))
    assert fs.factsheet_hash == factsheet_from_inputs(inputs).factsheet_hash


def test_tire_fact_table_matches_the_6f_contract_doc():
    """docs/tire_facts_contract.md is what Phase 7A builds from: same IDs, same sources, same order."""
    from app.ai.factsheet import TIRE_FACTS
    from tests.test_tire_facts_contract import DOC, facts

    doc = facts(DOC.read_text(encoding="utf-8"))
    code = {fid: ptr.strip("/").replace("/", ".") for fid, ptr, *_ in TIRE_FACTS}
    code = {fid: re.sub(r"\.(\d+)(?=\.|$)", r"[\1]", src) for fid, src in code.items()}
    assert code == doc


@pytest.mark.parametrize("where", ["metrics", "quality", "target"])
def test_a_new_numeric_field_in_the_findings_cannot_be_silently_dropped(where):
    inputs = json.loads(json.dumps(load(NAMES[0])))
    if where == "metrics":
        inputs["findings"]["corners"][0]["metrics"]["new_metric_diff_s"] = 0.1
    elif where == "target":
        inputs["findings"]["corners"][0]["target"]["new_value_kph"] = 200.0
    else:
        inputs["findings"]["quality"]["new_quality_s"] = 0.1
    with pytest.raises((KeyError, ValueError)):
        factsheet_from_inputs(inputs)
