"""
Phase 4 (Person 3): serving the trained model from `tires.py`.

The point of this file is that *both* paths are proven, not just the happy one. The
artifact is gitignored, so "no model" is the normal state of a fresh clone and has to
keep working; and the artifact records whether the model actually beat the trivial
baselines, which today it did not — so an artifact being present is not on its own a
reason to serve it.

Everything runs offline on the Phase 0 fixtures.

    cd backend && pytest -q tests/test_phase4_model_serving.py
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import tires as tires_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY, DRIVER = 9159, 16

sklearn = pytest.importorskip("sklearn", reason="model path needs sklearn to unpickle")
joblib = pytest.importorskip("joblib")


@pytest.fixture(autouse=True)
def _no_cached_artifact():
    """`_artifact` is lru_cached on purpose (load once, not per request), which means
    each test has to start from a clean slate."""
    tires_svc._artifact.cache_clear()
    yield
    tires_svc._artifact.cache_clear()


def _analyse(monkeypatch, **kw):
    monkeypatch.setattr(tires_svc, "get_client", lambda: FakeOpenF1Client())
    return asyncio.run(tires_svc.analyse_current_stint(SESSION_KEY, DRIVER, **kw))


def _write_artifact(tmp_path, *, beats_baseline: bool, **overrides):
    """A miniature but structurally real artifact: same pipeline shape, same keys,
    same `delta` target as ml/train.py writes."""
    import numpy as np
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    cat = ["compound", "circuit", "driver", "team"]
    num = ["tyre_age", "stint", "track_temp", "air_temp",
           "laps_since_safety_car", "gap_ahead_s"]
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame({
        "compound": rng.choice(["SOFT", "MEDIUM", "HARD"], n),
        "circuit": rng.choice(["Singapore Grand Prix", "Monaco Grand Prix"], n),
        "driver": rng.choice(["LEC", "SAI"], n),
        "team": rng.choice(["Ferrari", "Mercedes"], n),
        "tyre_age": rng.integers(0, 40, n).astype(float),
        "stint": rng.integers(1, 3, n).astype(float),
        "track_temp": rng.normal(36, 2, n),
        "air_temp": rng.normal(25, 2, n),
        "laps_since_safety_car": rng.integers(0, 40, n).astype(float),
        "gap_ahead_s": rng.normal(2.5, 1, n),
    })
    # a planted 0.05 s/lap so the served degradation is checkable, not just non-null
    df["delta"] = 0.05 * df["tyre_age"] + rng.normal(0, 0.05, n)

    pipe = Pipeline([
        ("pre", ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore"), cat),
                                   ("num", "passthrough", num)])),
        ("model", RandomForestRegressor(n_estimators=20, random_state=0)),
    ]).fit(df[num + cat], df["delta"])

    art = {
        "pipeline": pipe, "model_name": "tiny-rf", "target": "delta", "ref_laps": 5,
        "features": num + cat, "categorical_features": cat, "numeric_features": num,
        "feature_medians": {c: float(df[c].median()) for c in num},
        "metrics": {"mae": 0.25, "rmse": 0.3, "mae_std": 0.1, "folds": 3},
        "baseline_mae": {"persistence": 0.39, "stint_linear": 0.46},
        "beats_baseline": beats_baseline,
        "residual_quantiles": {"0.05": -0.4, "0.25": -0.1, "0.5": 0.0,
                               "0.75": 0.1, "0.95": 0.4},
    }
    art.update(overrides)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "tire_model.pkl"
    joblib.dump(art, path)
    return path


class _Boom:
    """A loadable pipeline that blows up on predict. Module level so joblib can
    pickle it."""

    def predict(self, X):
        raise RuntimeError("feature mismatch")


def _use(monkeypatch, path, *, force=False):
    monkeypatch.setattr(tires_svc.settings, "tire_model_path", str(path))
    monkeypatch.setattr(tires_svc.settings, "tire_model_force", force)


# ------------------------------------------------------------------ model absent

def test_missing_artifact_still_serves_the_baseline(monkeypatch, tmp_path):
    """A fresh clone has no .pkl (it's gitignored). The route must not care."""
    _use(monkeypatch, tmp_path / "does-not-exist.pkl")
    a = _analyse(monkeypatch)
    assert a.model_name == "baseline-linear"
    assert a.predictions and a.degradation_s_per_lap > 0


def test_unreadable_artifact_falls_back(monkeypatch, tmp_path):
    path = tmp_path / "tire_model.pkl"
    path.write_bytes(b"not a pickle")
    _use(monkeypatch, path)
    assert _analyse(monkeypatch).model_name == "baseline-linear"


def test_artifact_with_the_wrong_keys_falls_back(monkeypatch, tmp_path):
    """Trained by an older/other train.py — don't guess at what's missing."""
    path = tmp_path / "tire_model.pkl"
    joblib.dump({"pipeline": None, "model_name": "stale"}, path)
    _use(monkeypatch, path)
    assert _analyse(monkeypatch).model_name == "baseline-linear"


# -------------------------------------------------------------------- the gate

def test_model_that_lost_to_the_baseline_is_not_served(monkeypatch, tmp_path):
    """The state of the shipped artifact today (MODEL_CARD.md: beats_baseline=False).
    Present, loadable, and still not served."""
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=False))
    assert _analyse(monkeypatch).model_name == "baseline-linear"


def test_force_flag_overrides_the_gate(monkeypatch, tmp_path):
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=False), force=True)
    assert _analyse(monkeypatch).model_name == "tiny-rf"


# ------------------------------------------------------------------ model present

def test_winning_model_is_served_under_the_same_contract(monkeypatch, tmp_path):
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=True))
    a = _analyse(monkeypatch)

    assert a.model_name == "tiny-rf"
    # The artifact's frame plants +0.05 s/lap in tyre_age, so the served degradation
    # must be positive and in a plausible F1 range. Not asserted *equal* to 0.05: this
    # stint sits at the top of the trained age range and a forest flattens outside it,
    # which is the model's behaviour, not a bug in the serving path.
    assert 0.005 < a.degradation_s_per_lap < 0.15
    assert len(a.predictions) == 11
    later = a.predictions[-1]
    assert later.predicted_lap_time_s > a.predictions[0].predicted_lap_time_s
    # anchored on the stint's own reference pace, so still wall-clock seconds
    assert all(90 < p.predicted_lap_time_s < 110 for p in a.predictions)
    assert all(p.lower_s <= p.predicted_lap_time_s <= p.upper_s for p in a.predictions)
    assert a.recommended_pit_window is not None
    lo, hi = a.recommended_pit_window
    assert lo > a.tyre_age_laps, "still race lap numbers, not tyre age"


def test_confidence_comes_from_validation_error_not_this_stint(monkeypatch, tmp_path):
    """Two artifacts differing only in validation MAE must move model_confidence."""
    good = _write_artifact(tmp_path / "g", beats_baseline=True)
    _use(monkeypatch, good)
    sharp = _analyse(monkeypatch).model_confidence

    tires_svc._artifact.cache_clear()
    bad = _write_artifact(tmp_path / "b", beats_baseline=True,
                          metrics={"mae": 2.0, "rmse": 2.4, "mae_std": 1.0, "folds": 3})
    _use(monkeypatch, bad)
    assert _analyse(monkeypatch).model_confidence < sharp


def test_band_uses_the_artifacts_residual_quantiles(monkeypatch, tmp_path):
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=True))
    now = _analyse(monkeypatch).predictions[0]
    assert abs((now.predicted_lap_time_s - now.lower_s) - 0.4) < 0.01
    assert abs((now.upper_s - now.predicted_lap_time_s) - 0.4) < 0.01


def test_broken_pipeline_falls_back_mid_request(monkeypatch, tmp_path):
    """A loadable artifact whose pipeline raises on predict must not 500 the route."""
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=True, pipeline=_Boom()))
    a = _analyse(monkeypatch)
    assert a.model_name == "baseline-linear"
    assert a.predictions


# -------------------------------------------------------- OpenF1 -> training names

def test_openf1_labels_map_onto_training_categories():
    """OpenF1 says 'Singapore' / 'Marina Bay'; FastF1 trained on 'Singapore Grand
    Prix'. A miss is survivable but zeroes out the model's joint-top feature."""
    circuits = ["Singapore Grand Prix", "Monaco Grand Prix", "British Grand Prix"]
    assert tires_svc._match(circuits, "Singapore Grand Prix", "Singapore",
                            "Marina Bay") == "Singapore Grand Prix"
    assert tires_svc._match(circuits, None, "Singapore", "Marina Bay") == \
        "Singapore Grand Prix"
    # no vocabulary (unknown pipeline shape) — pass the label through, don't crash
    assert tires_svc._match(None, "Singapore") == "Singapore"
    # genuinely unknown stays unknown rather than snapping to a wrong circuit
    assert tires_svc._match(circuits, "Interlagos") == "Interlagos"


def test_feature_row_is_built_from_openf1(monkeypatch, tmp_path):
    art = joblib.load(_write_artifact(tmp_path, beats_baseline=True))
    fake = FakeOpenF1Client()
    stints = asyncio.run(fake.stints(session_key=SESSION_KEY, driver_number=DRIVER))
    stint = max(stints, key=lambda s: s["stint_number"])

    row = asyncio.run(tires_svc._feature_row(art, fake, SESSION_KEY, DRIVER, stint))

    assert set(row) == set(art["features"]) - {"tyre_age"}
    assert row["circuit"] == "Singapore Grand Prix"     # from country_name
    assert row["driver"] == "LEC" and row["team"] == "Ferrari"
    assert 25 < row["track_temp"] < 45                  # fixture weather, not the median
    assert row["gap_ahead_s"] == art["feature_medians"]["gap_ahead_s"]
