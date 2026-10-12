"""Phase 3 — findings on the real fixture, JSON safety, schema-doc sync and snapshot."""
from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path

import pytest

from app.ingestion.service import get_fastest_valid_lap
from app.processing.delta import compare_laps
from app.processing.insights import build_session_context, generate_findings, json_schema
from app.processing.models import LapData
from app.storage.repository import ParquetRepository

HERE = Path(__file__).parent
STORE = ParquetRepository(HERE / "fixtures" / "ingestion" / "store")
KEY = "2024_05_Q"
DRIVERS = ("VER", "PER", "ALO")
SCHEMA_DOC = HERE.parents[1] / "docs" / "insight_schema.md"
SNAPSHOT = HERE / "fixtures" / "insights" / "2024_05_Q_ALO_vs_VER.json"


def _report(reference: str, target: str):
    ref, tgt = get_fastest_valid_lap(STORE, KEY, reference), get_fastest_valid_lap(STORE, KEY, target)
    cmp = compare_laps(LapData.from_repository(STORE, KEY, ref.driver, ref.lap_number),
                       LapData.from_repository(STORE, KEY, tgt.driver, tgt.lap_number))
    return generate_findings(cmp, STORE.get_corners(KEY),
                             context=build_session_context(ref, tgt, STORE.get_session_info(KEY)))


@pytest.mark.parametrize("reference, target", list(itertools.permutations(DRIVERS, 2)))
def test_tiling_invariant_on_every_fixture_pair(reference, target):
    report = _report(reference, target)
    total = report.lap_summary.time_loss_s
    assert abs(sum(f.time_loss_s for f in report.corners) - total) <= 1e-9
    assert report.quality.tiling_error_s <= 1e-9
    official = report.session_context.target.lap_time_s - report.session_context.reference.lap_time_s
    assert total == pytest.approx(official, abs=0.02)
    # every official Shanghai corner is matched to a real speed minimum and passes the X/Y check
    official_corners = [f for f in report.corners if f.corner_number is not None]
    assert len(official_corners) == 16
    assert all("X/Y" not in " ".join(f.notes) for f in official_corners)
    json.dumps(report.model_dump(mode="json"), allow_nan=False)


def test_session_context_carries_tyres_weather_and_status():
    ctx = _report("VER", "ALO").session_context
    assert (ctx.session_key, ctx.session_code, ctx.event_name) == (KEY, "Q", "Chinese Grand Prix")
    for lap in (ctx.reference, ctx.target):
        assert lap.compound == "SOFT" and lap.tyre_age_laps is not None
        assert lap.track_temp_c is not None and lap.air_temp_c is not None
        assert lap.track_status_flags == ["green"]
    assert any("traffic data unavailable" in note for note in ctx.notes)


def test_schema_doc_matches_pydantic_model():
    text = SCHEMA_DOC.read_text(encoding="utf-8")
    block = re.search(r"<!-- schema:start -->\s*```json\n(.*?)\n```\s*<!-- schema:end -->", text, re.S)
    assert block, "docs/insight_schema.md is missing the generated schema block"
    assert json.loads(block.group(1)) == json_schema(), (
        "schema drifted: regenerate with `python -m app.processing.insights` and paste into docs/insight_schema.md"
    )


def test_snapshot_alo_vs_ver():
    """Reviewed manually once. Regenerate deliberately with UPDATE_SNAPSHOTS=1."""
    current = json.loads(json.dumps(_report("VER", "ALO").model_dump(mode="json"), allow_nan=False))
    if os.getenv("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip("snapshot written; review it and commit")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    _assert_close(current, expected, "$")


def _assert_close(a: object, b: object, path: str) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), f"{path}: keys differ"
        for key in a:
            _assert_close(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{path}: length differs"
        for i, (x, y) in enumerate(zip(a, b)):
            _assert_close(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a == pytest.approx(b, abs=1e-6), path
    else:
        assert a == b, path
