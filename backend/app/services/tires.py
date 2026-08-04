"""
Tire-degradation analysis (Person 3).

Ships a **baseline** that works today: fit a line to a stint's clean, *fuel-corrected*
lap times vs tyre age → degradation rate (s/lap), extrapolate future pace, estimate a
pit window and a confidence band from the fit residuals.

Units, so nothing downstream has to guess (Phase 1):

* `degradation_s_per_lap` — slope of **fuel-corrected** lap time vs tyre age. Fuel
  burn-off makes a car faster over a stint; without correcting for it the served
  figure is degradation *minus* fuel effect and can even come out negative.
* `LapPacePrediction.predicted_lap_time_s` and its band — **raw wall-clock** seconds,
  i.e. fuel-corrected prediction un-corrected on the way out, so the frontend shows
  the lap time a stopwatch would show.
* `recommended_pit_window` — **race lap numbers** (see the schema doc comment).
* `remaining_competitive_laps` — a count of laps from now.

Phase 4 adds the trained model (`ml/train.py` → `ml/models/tire_model.pkl`) *behind a
gate*: the artifact records whether it actually beat the trivial baselines, and
`_artifact()` refuses to serve one that didn't. Today it didn't (see
`ml/models/MODEL_CARD.md`), so `model_name` stays `"baseline-linear"` and nothing about
the response changes. Retrain a winner and it is served with no code change. The
endpoint shape (TireAnalysis) is identical either way, so the frontend never changes.
"""
from __future__ import annotations

import difflib
import logging
from functools import lru_cache

import numpy as np
import pandas as pd

from ..clients.openf1 import get_client
from ..config import settings
from ..models.schemas import LapPacePrediction, TireAnalysis

log = logging.getLogger(__name__)

# pace within this many seconds of the stint's best is still "competitive"
COMPETITIVE_THRESHOLD_S = 1.2
# seconds/lap the car gains as fuel burns off. MUST match ml/build_dataset.py, or the
# model trains on one definition of "corrected" and serves another.
# tests/test_phase1_tires.py asserts the two stay in sync.
FUEL_EFFECT_S_PER_LAP = 0.03
# laps beyond the local median, in MADs, before a lap is treated as safety-car/traffic
OUTLIER_MAD_K = 5.0
# clean laps needed for full sample-size credit in `_confidence`
CONFIDENT_SAMPLE_LAPS = 12
# keys `_artifact` requires before it will trust a .pkl
ARTIFACT_KEYS = frozenset({"pipeline", "features", "categorical_features", "model_name",
                           "metrics", "residual_quantiles", "beats_baseline"})
# Fallback values for the two features we do not fetch per request. Medians of
# ml/data/stints.parquet (2023-24, 39387 laps); artifacts written after this change
# carry their own `feature_medians` and those win. Held constant across the horizon
# either way, and `_model_curve` anchors on the stint's own reference pace, so the
# level they imply cancels — only the model's response to `tyre_age` survives.
IMPUTED_FEATURES = {"gap_ahead_s": 2.523, "laps_since_safety_car": 23.0,
                    "track_temp": 36.1, "air_temp": 25.3}


async def analyse_current_stint(session_key: int | str, driver_number: int,
                                horizon: int = 10,
                                stint_number: int | None = None) -> TireAnalysis:
    client = get_client()
    laps = await client.laps(session_key=session_key, driver_number=driver_number)
    stints = await client.stints(session_key=session_key, driver_number=driver_number)
    pits = await client.pit(session_key=session_key, driver_number=driver_number)
    skey = _session_key_int(session_key, laps)
    if not stints:
        return _empty(skey, driver_number)

    if stint_number is None:
        stint = max(stints, key=lambda s: s.get("stint_number") or 0)  # current/last
    else:
        stint = next((s for s in stints if s.get("stint_number") == stint_number), None)
        if stint is None:
            return _empty(skey, driver_number)

    lap_start = stint.get("lap_start") or 1
    lap_end = stint.get("lap_end") or 10**6      # current stint has no end yet
    age0 = stint.get("tyre_age_at_start") or 0

    # In-laps (the lap the car pitted on) and out-laps are both unrepresentative.
    # `is_pit_out_lap` only ever covers the latter, and not always.
    in_laps = {p["lap_number"] for p in pits if p.get("lap_number") is not None}
    excluded = in_laps | {n + 1 for n in in_laps}

    clean = [
        l for l in laps
        if l.get("lap_duration") and not l.get("is_pit_out_lap")
        and lap_start <= (l.get("lap_number") or -1) <= lap_end
        and l["lap_number"] not in excluded
    ]
    if len(clean) < 3:
        return _empty(skey, driver_number, compound=stint.get("compound"),
                      tyre_age=age0)

    lap_nums = np.array([l["lap_number"] for l in clean], dtype=float)
    # age is derived from lap_number, so gaps in the stint's lap range are fine
    ages = np.array([age0 + (l["lap_number"] - lap_start) for l in clean], dtype=float)
    raw = np.array([l["lap_duration"] for l in clean], dtype=float)

    current_lap = int(lap_nums.max())
    current_age = int(ages.max())
    # fuel-correct to a current-lap reference: at `current_lap` the correction is 0,
    # so predictions for "now" need no un-correcting.
    corrected = raw + FUEL_EFFECT_S_PER_LAP * (lap_nums - current_lap)

    keep = _outlier_mask(corrected)
    dropped = int((~keep).sum())
    if dropped:
        log.info("tires: dropped %d anomalous lap(s) from the fit (driver %s, stint %s)",
                 dropped, driver_number, stint.get("stint_number"))
    if int(keep.sum()) < 3:   # nearly everything looked anomalous — trust nothing
        return _empty(skey, driver_number, compound=stint.get("compound"),
                      tyre_age=current_age)
    ages, corrected = ages[keep], corrected[keep]

    slope, intercept, resid_std = _baseline_fit(ages, corrected)
    best = float(corrected.min())

    # Fuel-corrected predicted lap times over the horizon, plus the band offsets.
    # The baseline extrapolates its own straight line and widens by the spread of
    # *this* stint's residuals; the model path replaces both below.
    ks = np.arange(horizon + 1, dtype=float)
    mu = slope * (current_age + ks) + intercept
    lo_off = hi_off = 1.96 * resid_std + 0.01 * ks
    model_name = "baseline-linear"
    confidence = _confidence(resid_std, len(ages))

    art = _artifact()
    if art is not None:
        curve = await _model_curve(art, client, session_key, driver_number, stint,
                                   ages, corrected, current_age, ks)
        if curve is not None:
            mu, model_name = curve, art["model_name"]
            # held-out residual quantiles from walk-forward validation — a band that
            # was measured, not assumed. Still widened with horizon: those residuals
            # were scored one lap ahead, not ten.
            q = art["residual_quantiles"]
            lo_off = np.maximum(0.0, -float(q["0.05"])) + 0.01 * ks
            hi_off = np.maximum(0.0, float(q["0.95"])) + 0.01 * ks
            # the model's *validation* MAE, so the number means something outside
            # this stint. Full sample credit: it was measured over 40 races.
            confidence = _confidence(float(art["metrics"]["mae"]), CONFIDENT_SAMPLE_LAPS)
            slope, intercept = (float(v) for v in np.polyfit(current_age + ks, mu, 1))

    preds = [
        LapPacePrediction(
            laps_ahead=int(k),
            # back to raw wall-clock seconds
            predicted_lap_time_s=round(float(m + FUEL_EFFECT_S_PER_LAP * k), 3),
            lower_s=round(float(m + FUEL_EFFECT_S_PER_LAP * k - lo), 3),
            upper_s=round(float(m + FUEL_EFFECT_S_PER_LAP * k + hi), 3))
        for k, m, lo, hi in zip(ks, mu, lo_off, hi_off)
    ]

    remaining = _remaining_competitive(slope, intercept, current_age, best)
    if remaining is None:
        log.info("tires: slope %.4f s/lap <= 0 (warm-up or improving track) — "
                 "no pit window for driver %s", slope, driver_number)
    # RACE LAP NUMBERS, not tyre age — see the TireAnalysis doc comment in schemas.py
    pit_window = (current_lap + max(1, remaining - 2), current_lap + remaining + 2) \
        if remaining is not None else None

    return TireAnalysis(
        session_key=skey, driver_number=driver_number,
        compound=stint.get("compound"), tyre_age_laps=current_age,
        degradation_s_per_lap=round(float(slope), 3),
        remaining_competitive_laps=remaining,
        recommended_pit_window=pit_window,
        model_confidence=confidence, predictions=preds,
        model_name=model_name,
    )


# ------------------------------------------------------------------ trained model

@lru_cache(maxsize=1)
def _artifact() -> dict | None:
    """The trained pipeline, or None to stay on the baseline. Loaded once.

    Returns None — and logs why — on every failure mode, because the endpoint must
    never 500 over a missing `.pkl`:

    * no file (the default for a fresh clone: the artifact is gitignored),
    * unreadable file, or one written by a different `train.py` (missing keys),
    * `beats_baseline=False`. This is the important one. `ml/train.py` scores the
      model against two trivial baselines and records the verdict; today it lost
      (MODEL_CARD.md), so serving it would make the endpoint worse. Set
      `RACEIQ_TIRE_MODEL_FORCE=1` to serve it anyway.
    """
    path = settings.tire_model_path
    try:
        import joblib
        art = joblib.load(path)
        missing = ARTIFACT_KEYS - set(art)
        if missing:
            raise KeyError(f"artifact is missing {sorted(missing)}")
    except Exception as e:  # noqa: BLE001 — any failure means "use the baseline"
        log.info("tires: no usable model at %s (%s: %s) — serving baseline-linear",
                 path, type(e).__name__, e)
        return None
    if not art["beats_baseline"] and not settings.tire_model_force:
        log.info("tires: model %r (MAE %.3f s) does not beat the baselines %s — "
                 "serving baseline-linear. Set RACEIQ_TIRE_MODEL_FORCE=1 to override.",
                 art["model_name"], art["metrics"]["mae"], art.get("baseline_mae"))
        return None
    log.info("tires: serving %r from %s", art["model_name"], path)
    return art


async def _model_curve(art: dict, client, session_key, driver_number: int, stint: dict,
                       ages: np.ndarray, corrected: np.ndarray, current_age: int,
                       ks: np.ndarray) -> np.ndarray | None:
    """Fuel-corrected predicted lap times over the horizon, or None to fall back.

    The artifact's target is `delta` — seconds above the stint's *own* reference pace
    (median of its first `ref_laps` clean fuel-corrected laps), not an absolute lap
    time. So the model supplies only the shape in tyre age and this stint's measured
    reference supplies the level:

        predicted(age) = ref_s + [model(age) - model(ref_age)]

    Subtracting the model's own value at the reference age is what makes the two
    imputed features (and any constant offset the model carries for this circuit)
    cancel instead of landing seconds out — the failure mode MODEL_CARD.md documents
    for the absolute target.
    """
    try:
        ref_laps = int(art.get("ref_laps", 5))
        first = np.argsort(ages)[:ref_laps]
        ref_age, ref_s = float(np.median(ages[first])), float(np.median(corrected[first]))

        row = await _feature_row(art, client, session_key, driver_number, stint)
        frame = pd.DataFrame([{**row, "tyre_age": a}
                              for a in np.concatenate(([ref_age], current_age + ks))])
        pred = np.asarray(art["pipeline"].predict(frame[art["features"]]), dtype=float)
        return ref_s + pred[1:] - pred[0]
    except Exception as e:  # noqa: BLE001 — a bad feature row must not 500 the route
        log.warning("tires: model prediction failed (%s: %s) — falling back to "
                    "baseline-linear", type(e).__name__, e)
        return None


async def _feature_row(art: dict, client, session_key, driver_number: int,
                       stint: dict) -> dict:
    """One row of training-time columns, built from OpenF1 at request time.

    `tyre_age` is filled in per horizon step by the caller. `gap_ahead_s` and
    `laps_since_safety_car` are imputed rather than fetched: both would cost extra
    OpenF1 calls per request (intervals, race control) against a 3 req/s limit, and
    the reference-anchoring above cancels the level they imply.
    """
    ses = await client.sessions(session_key=session_key)
    drivers = await client.drivers(session_key=session_key, driver_number=driver_number)
    wx = await client.weather(session_key)
    s, d = (ses[0] if ses else {}), (drivers[0] if drivers else {})
    med = {**IMPUTED_FEATURES, **art.get("feature_medians", {})}
    vocab = _known_categories(art)
    country = s.get("country_name")

    return {
        "compound": stint.get("compound"),
        # training used FastF1's EventName ("Singapore Grand Prix"); OpenF1 has none
        "circuit": _match(vocab.get("circuit"), f"{country} Grand Prix" if country
                          else None, country, s.get("circuit_short_name")),
        "driver": _match(vocab.get("driver"), d.get("name_acronym")),
        "team": _match(vocab.get("team"), d.get("team_name")),
        "stint": float(stint.get("stint_number") or 1),
        "track_temp": _mean(wx, "track_temperature", med["track_temp"]),
        "air_temp": _mean(wx, "air_temperature", med["air_temp"]),
        "laps_since_safety_car": med["laps_since_safety_car"],
        "gap_ahead_s": med["gap_ahead_s"],
    }


def _known_categories(art: dict) -> dict[str, list]:
    """The one-hot encoder already stores every category it was fitted on, so the
    artifact doesn't need to repeat them."""
    try:
        enc = art["pipeline"].named_steps["pre"].named_transformers_["cat"]
        return dict(zip(art["categorical_features"],
                        (list(c) for c in enc.categories_)))
    except Exception:  # noqa: BLE001 — a different pipeline shape just means no vocab
        return {}


def _match(vocab: list | None, *candidates: str | None) -> str | None:
    """Best-effort map of an OpenF1 label onto a training-time category.

    OpenF1 and FastF1 disagree on spelling ("Singapore" / "Marina Bay" vs
    "Singapore Grand Prix"). `handle_unknown="ignore"` means a miss is survivable,
    but it silently zeroes out the model's joint-top feature, so a fuzzy match is
    worth the three lines."""
    first = next((c for c in candidates if c), None)
    if not vocab:
        return first
    for c in candidates:
        if not c:
            continue
        if c in vocab:
            return c
        hit = difflib.get_close_matches(c, vocab, n=1, cutoff=0.5)
        if hit:
            return hit[0]
    log.info("tires: no training category matches %s — the model will see it as "
             "unseen", candidates)
    return first


def _mean(rows: list[dict], field: str, default: float) -> float:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return float(np.mean(vals)) if vals else default


def _session_key_int(session_key: int | str, laps: list[dict]) -> int:
    """OpenF1 accepts `session_key="latest"`; the response schema needs a real int."""
    if str(session_key).lstrip("-").isdigit():
        return int(session_key)
    return int(laps[0]["session_key"]) if laps else 0


def _baseline_fit(ages: np.ndarray, times: np.ndarray) -> tuple[float, float, float]:
    slope, intercept = np.polyfit(ages, times, 1)
    resid = times - (slope * ages + intercept)
    return float(slope), float(intercept), float(np.std(resid) or 0.05)


def _outlier_mask(times: np.ndarray) -> np.ndarray:
    """True for laps to keep. Safety-car / traffic laps are *dropped*, never
    interpolated over — inventing a lap time is the same sin the AI layer is
    forbidden from committing."""
    med = np.median(times)
    mad = np.median(np.abs(times - med)) or 0.1
    return np.abs(times - med) < OUTLIER_MAD_K * mad


def _remaining_competitive(slope: float, intercept: float, current_age: int,
                           best: float) -> int | None:
    if slope <= 0:
        return None  # tyres not degrading (or warming up) — no window yet
    for k in range(0, 40):
        if slope * (current_age + k) + intercept > best + COMPETITIVE_THRESHOLD_S:
            return k
    return 40


def _confidence(resid_std: float, n_laps: int) -> float:
    """Tight residuals *and* enough laps to believe the trend. A 3-lap fit must not
    report 0.97 no matter how neatly those 3 points line up."""
    fit = 1.0 / (1.0 + resid_std * 3.0)
    sample = min(1.0, n_laps / CONFIDENT_SAMPLE_LAPS)
    return round(float(np.clip(fit * sample, 0.1, 0.97)), 2)


def _empty(session_key: int, driver_number: int, compound=None,
           tyre_age=None) -> TireAnalysis:
    return TireAnalysis(
        session_key=session_key, driver_number=driver_number,
        compound=compound, tyre_age_laps=tyre_age,
        degradation_s_per_lap=0.0, remaining_competitive_laps=None,
        recommended_pit_window=None, model_confidence=0.1, predictions=[],
        model_name="baseline-linear",
    )
