"""Phase 6E — registry, prediction service (cut rule, insufficient data, contract) and API."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tire_service
from app.api.routes.tires import to_schema
from app.main import app
from app.tires.registry import ChecksumMismatchError, ModelNotAvailableError, ModelRegistry
from app.tires.service import TireService
from tests.tire_service_fixtures import QUALI, RACE, TOTAL_LAPS, FakeRepo, PersistencePoint, lap_records, write_registry


def service(tmp_path, **kw) -> TireService:
    return TireService(FakeRepo(), ModelRegistry(write_registry(tmp_path, **kw)))


# ---------------------------------------------------------------- registry
def test_registry_loads_lazily_verifies_and_caches(tmp_path):
    reg = ModelRegistry(write_registry(tmp_path))
    entry = reg.production()
    first = reg.load(entry)
    assert reg.load(entry) is first and set(first.step_quantiles) == set(range(1, 26))


def test_checksum_mismatch_raises(tmp_path):
    reg = ModelRegistry(write_registry(tmp_path))
    artifact = tmp_path / "tires" / "m" / "v1" / "model.json"
    artifact.write_text(artifact.read_text(encoding="utf-8").replace("0.05", "0.5", 1), encoding="utf-8")
    with pytest.raises(ChecksumMismatchError):
        reg.load(reg.production())


@pytest.mark.parametrize("breakage", ["no_registry", "no_artifact", "no_production", "escape"])
def test_missing_model_is_model_not_available(tmp_path, breakage):
    path = write_registry(tmp_path, status="candidate" if breakage == "no_production" else "production")
    if breakage == "no_registry":
        path.unlink()
    elif breakage == "no_artifact":
        (tmp_path / "tires" / "m" / "v1" / "residuals.npz").unlink()
    elif breakage == "escape":
        path.write_text(path.read_text(encoding="utf-8").replace("tires/m/v1/model.json", "../outside.json"), encoding="utf-8")
    reg = ModelRegistry(path)
    with pytest.raises(ModelNotAvailableError):
        reg.load(reg.production())


# ---------------------------------------------------------------- the cut rule
def test_changing_laps_after_the_cut_leaves_the_output_unchanged(tmp_path):
    lap = 30
    before = service(tmp_path).predict(RACE, "AAA", lap, 10)
    mutated = []
    for rec in lap_records():
        if rec.lap_number > lap:   # every driver's future: times, tyres, stints, a safety car, rain, temperatures
            rec = rec.model_copy(update={"lap_time_s": 140.0, "compound": "SOFT", "stint": 9, "tyre_age": 1,
                                         "track_status": "14", "rainfall": True, "track_temp_c": 10.0,
                                         "pit_in_time_s": 1.0, "is_accurate": False})
        mutated.append(rec)
    after = TireService(FakeRepo(mutated), ModelRegistry(tmp_path / "registry.json")).predict(RACE, "AAA", lap, 10)
    assert before.status == "ok" and before == after
    changed_history = [r.model_copy(update={"lap_time_s": r.lap_time_s + 0.5}) if r.driver == "AAA" and 25 <= r.lap_number <= lap
                       else r for r in lap_records()]
    poked = TireService(FakeRepo(changed_history), ModelRegistry(tmp_path / "registry.json")).predict(RACE, "AAA", lap, 10)
    assert poked.predicted_laps != before.predicted_laps  # the history is used (not a vacuous pass)


def test_three_cut_laps_of_one_stint_see_only_their_own_history(tmp_path):
    svc = service(tmp_path)
    outs = {lap: svc.predict(RACE, "AAA", lap, 5) for lap in (24, 30, 36)}
    assert [o.clean_laps_used for o in outs.values()] == [5, 11, 17]   # stint 2: out-lap 19 excluded, 20…lap clean
    assert [o.tyre_age for o in outs.values()] == [6, 12, 18]
    for lap, o in outs.items():
        assert [p.lap for p in o.predicted_laps] == list(range(lap + 1, min(lap + 5, TOTAL_LAPS) + 1))  # capped at the flag


# ---------------------------------------------------------------- insufficient data
def test_four_clean_laps_is_insufficient_data_without_predictions_or_window(tmp_path):
    p = service(tmp_path).predict(RACE, "AAA", 23, 5)  # stint 2: out-lap 19 excluded → laps 20–23 = 4 clean
    assert p.status == "insufficient_data" and p.clean_laps_used == 4 and "at least 5" in (p.reason or "")
    assert p.predicted_laps is None and p.pit_window is None and p.degradation_s_per_lap is None
    assert service(tmp_path / "b").predict(RACE, "AAA", 24, 5).status == "ok"


# ---------------------------------------------------------------- contract
@pytest.mark.parametrize("lap, horizon", [(8, 1), (12, 5), (30, 25), (37, 10), (39, 5)])
def test_contract_finite_ordered_consecutive(tmp_path, lap, horizon):
    p = service(tmp_path).predict(RACE, "BBB", lap, horizon)
    body = to_schema(p).model_dump(mode="json")
    json.dumps(body, allow_nan=False)  # no NaN / Inf anywhere
    laps = [x["lap"] for x in body["predicted_laps"]]
    assert laps == list(range(lap + 1, lap + 1 + min(horizon, TOTAL_LAPS - lap)))
    assert all(x["lo_s"] <= x["pred_s"] <= x["hi_s"] for x in body["predicted_laps"])
    if body["pit_window"] is None:
        assert body["pit_window_reason"] and any(w.startswith("pit_window") for w in body["warnings"])
    else:
        w = body["pit_window"]
        got = [v for v in (w["earliest_lap"], w["central_lap"], w["latest_lap"]) if v is not None]
        assert got == sorted(got) and all(lap < v <= TOTAL_LAPS for v in got)


def test_pit_window_and_degradation_on_a_tight_model(tmp_path):
    p = service(tmp_path, spread=0.05).predict(RACE, "BBB", 12, 5)
    w = p.pit_window
    assert w is not None and w["pace_basis"] == "fuel_adjusted_model_term" and w["central_lap"] is not None
    assert p.remaining_competitive_laps == w["central_lap"] - 12
    assert p.degradation_s_per_lap == 0.04 and p.degradation_ci == (0.03, 0.05)
    assert "ignores safety-car probability" in w["assumptions"]


def test_a_model_without_degradation_reports_null_with_a_warning(tmp_path):
    p = service(tmp_path, point=PersistencePoint()).predict(RACE, "BBB", 12, 5)
    assert p.degradation_s_per_lap is None and p.degradation_ci is None
    assert any(w.startswith("degradation_s_per_lap") for w in p.warnings)
    assert p.pit_window is None or p.pit_window["pace_basis"] == "raw"


def test_responses_are_cached_per_model_version(tmp_path):
    svc = service(tmp_path)
    a = svc.predict(RACE, "BBB", 20, 5)
    assert svc.predict(RACE, "bbb", 20, 5) is a and svc.predict(RACE, "11", 20, 5) is a  # code or number
    assert svc.predict(RACE, "BBB", 20, 6) is not a


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    svc = service(tmp_path)
    app.dependency_overrides[get_tire_service] = lambda: svc
    yield TestClient(app)
    app.dependency_overrides.pop(get_tire_service, None)


def test_api_predict_ok(client):
    r = client.get("/api/v1/tires/predict", params={"session": "2024-05-R", "driver": "BBB", "lap": 20, "horizon": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["session_key"] == "2024-05-R" and len(body["predicted_laps"]) == 3
    assert body["model"] == {"name": "test_model", "version": "v1", "trained_through": "2024-04-21"}


@pytest.mark.parametrize("params, status, code", [
    ({"driver": "ZZZ", "lap": 20}, 404, "driver_not_found"),
    ({"driver": "BBB", "lap": 77}, 404, "lap_not_found"),
    ({"driver": "BBB", "lap": 20, "session": "2024-05-Q"}, 422, "not_a_race_session"),
    ({"driver": "BBB", "lap": 20, "session": "2024-09-R"}, 404, "session_not_found"),
    ({"driver": "BBB", "lap": 20, "horizon": 26}, 422, "validation_error"),
    ({"driver": "BBB", "lap": 20, "horizon": 0}, 422, "validation_error"),
])
def test_api_errors(client, params, status, code):
    r = client.get("/api/v1/tires/predict", params={"session": "2024-05-R", "horizon": 5, **params})
    assert r.status_code == status and r.json()["error"]["code"] == code
    if code == "not_a_race_session":
        assert "tire forecasting requires a race session" in r.json()["error"]["message"]


def test_api_model_missing_is_503(tmp_path):
    svc = TireService(FakeRepo(), ModelRegistry(tmp_path / "nowhere" / "registry.json"))
    app.dependency_overrides[get_tire_service] = lambda: svc
    try:
        c = TestClient(app)
        r = c.get("/api/v1/tires/predict", params={"session": "2024-05-R", "driver": "BBB", "lap": 20})
        assert r.status_code == 503 and r.json()["error"]["code"] == "model_not_available"
        assert c.get("/api/v1/tires/models").status_code == 503
    finally:
        app.dependency_overrides.pop(get_tire_service, None)


def test_api_models_lists_versions_and_metrics(client):
    body = client.get("/api/v1/tires/models").json()
    assert body["models"][0]["status"] == "production" and body["models"][0]["metrics"]["holdout_h5_mae"] == 0.5


def test_committed_registry_loads_and_verifies():
    """The registry shipped in models/ must load with matching checksums."""
    from app.config import settings

    reg = ModelRegistry(settings.tire_registry_path)
    entries = reg.entries()
    assert sum(e.status == "production" for e in entries) == 1
    for e in entries:
        m = reg.load(e)
        assert m.step_quantiles and all(math.isfinite(q) for q in m.step_quantiles.values())
        assert np.all(np.diff([m.step_quantiles[s] for s in sorted(m.step_quantiles)]) > -0.2)


def test_recent_anchored_fixed_effects_takes_its_level_from_the_last_three_laps():
    import pandas as pd

    from tests.tire_service_fixtures import fixed_effects

    full = fixed_effects()
    recent = type(full)(**{**full.__dict__, "anchor_laps": 3})
    hist = pd.DataFrame({"lap_number": range(1, 9), "compound": "HARD", "tyre_age": range(1, 9),
                         "fuel_proxy": range(39, 31, -1), "track_temp_c": 40.0,
                         "lap_time_s": [91.0, 91.0, 91.0, 91.0, 91.0, 92.0, 92.0, 92.0]})
    fut = pd.DataFrame({"lap_number": [9], "compound": "HARD", "tyre_age": [9], "fuel_proxy": [31], "track_temp_c": 40.0})
    step = 0.04 * 1 - 0.05 * 1  # one more lap of age, one less of fuel
    assert recent.point(hist, fut)[0] == pytest.approx(92.0 + 2 * step)  # level = last 3 laps (centred on lap 7)
    assert full.point(hist, fut)[0] < recent.point(hist, fut)[0]      # all-history level lags the jump
    assert recent.kind == "pooled_fixed_effects_recent" and full.kind == "pooled_fixed_effects"


# ---------------------------------------------------------------- 6F additions: what the panel plots
def test_history_is_the_stint_up_to_the_cut_with_exclusions_and_fits(tmp_path):
    p = service(tmp_path).predict(RACE, "AAA", 30, 5)
    assert p.history is not None and [h.lap for h in p.history] == list(range(19, 31))  # stint 2 = laps 19…
    out_lap = p.history[0]
    assert not out_lap.clean and out_lap.excluded_reason == "in_or_out_lap" and out_lap.fitted_s is None
    assert all(h.clean and h.fitted_s is not None for h in p.history[1:])
    assert p.clean_laps_used == sum(h.clean for h in p.history) and p.race_laps_total == TOTAL_LAPS


def test_threshold_curve_spans_stint_start_to_horizon_end_on_the_raw_scale(tmp_path):
    p = service(tmp_path).predict(RACE, "BBB", 12, 5)
    curve = p.threshold_curve
    assert curve is not None and curve[0].lap == 1 and curve[-1].lap == 12 + 25
    w = p.pit_window
    assert w is not None
    # raw threshold(l) = best + X + β_fuel·fuel(l): it falls by β_fuel (0.05) per lap as fuel burns
    assert curve[1].threshold_s - curve[0].threshold_s == pytest.approx(-0.05, abs=1e-3)
    at_cut = next(c for c in curve if c.lap == 12)
    assert at_cut.threshold_s == pytest.approx(w["threshold_pace_s"] + 0.05 * (TOTAL_LAPS - 12), abs=2e-3)
    assert w["confidence_reasons"]


def test_insufficient_data_carries_a_reason_code_and_the_requirement(tmp_path):
    p = service(tmp_path).predict(RACE, "AAA", 22, 5)
    assert p.status == "insufficient_data" and p.reason_code == "too_few_clean_laps"
    assert (p.clean_laps_used, p.clean_laps_required) == (3, 5)
    assert p.history is not None and p.threshold_curve is None and p.predicted_laps is None
    wet = [r.model_copy(update={"compound": "INTERMEDIATE"}) if r.driver == "BBB" and 1 <= r.lap_number <= 40 else r
           for r in lap_records()]
    q = TireService(FakeRepo(wet), ModelRegistry(tmp_path / "registry.json")).predict(RACE, "BBB", 20, 5)
    assert q.status == "insufficient_data" and q.reason_code == "wet_compound"
