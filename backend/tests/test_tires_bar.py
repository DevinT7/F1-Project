"""Phase 6D — the agreed success bar applied mechanically."""
from __future__ import annotations

import pandas as pd

from app.tires.bar import check


def frames(model_mae=0.30, model_bias=0.01, model_wf=0.35, wins=(0.2, 0.2, 0.5), coverage=float("nan")):
    summary = pd.DataFrame([
        {"model": "persistence", "split": "loro", "horizon": 5, "mae": 0.37, "bias": 0.0, "coverage": float("nan")},
        {"model": "persistence", "split": "walk_forward", "horizon": 5, "mae": 0.39, "bias": 0.0, "coverage": float("nan")},
        {"model": "m", "split": "loro", "horizon": 5, "mae": model_mae, "bias": model_bias, "coverage": coverage},
        {"model": "m", "split": "walk_forward", "horizon": 5, "mae": model_wf, "bias": 0.0, "coverage": coverage},
    ])
    by_race = pd.DataFrame(
        [{"model": "persistence", "split": "loro", "horizon": 5, "session_key": k, "mae": 0.37} for k in "abc"]
        + [{"model": "m", "split": "loro", "horizon": 5, "session_key": k, "mae": v} for k, v in zip("abc", wins)])
    return summary, by_race


def test_a_model_meeting_every_line_passes():
    [v] = check(*frames())
    assert v.passes and v.races_won == 2 and v.loro_target == 0.9 * 0.37
    assert v.checks["5_interval_coverage_90_97"] is None  # no intervals: not applicable, not a failure


def test_each_line_can_fail_on_its_own():
    assert not check(*frames(model_mae=0.34))[0].checks["1_mae_10pct_below_persistence"]
    assert not check(*frames(model_bias=-0.06))[0].checks["2_bias_within_0.05"]
    assert not check(*frames(model_wf=0.40))[0].checks["3_walk_forward_not_worse"]
    assert not check(*frames(wins=(0.2, 0.5, 0.5)))[0].checks["4_wins_majority_of_races"]
    v = check(*frames(coverage=0.85))[0]
    assert v.checks["5_interval_coverage_90_97"] is False and not v.passes


def test_holdout_bar_uses_absolute_numbers_and_nominal_coverage():
    import pandas as pd

    from app.tires.bar import check_holdout

    rows = [("persistence", 0.37, 0.0, None), ("m+conformal80", 0.30, 0.01, 0.80), ("m+conformal95", 0.30, 0.01, 0.80),
            ("slow", 0.34, 0.0, None)]
    summary = pd.DataFrame([{"model": m, "split": "holdout", "horizon": 5, "mae": a, "bias": b, "coverage": c}
                            for m, a, b, c in rows])
    by_race = pd.DataFrame([{"model": m, "split": "holdout", "horizon": 5, "session_key": f"r{i}", "mae": a + 0.01 * i}
                            for m, a, _, _ in rows for i in range(3)])
    v = {x.model: x for x in check_holdout(summary, by_race)}
    assert v["m+conformal80"].passes                     # 80% interval judged against 72–88%
    assert not v["m+conformal95"].passes                 # 95% interval at 80% coverage fails 90–97%
    assert not v["slow"].checks["1_mae_10pct_below_persistence"]  # 0.34 > 0.332 absolute


def test_holdout_split_trains_on_all_development_races():
    from app.tires.evaluation import race_order, split_generator
    from app.tires.synthetic import make_race

    frame = pd.concat([make_race(seed=i, session_key=f"2023_{i + 1:02d}_R") for i in range(6)], ignore_index=True)
    folds = list(split_generator(frame, "holdout"))
    order = race_order(frame)
    assert [f.test_key for f in folds] == order[-3:]
    assert all(f.train_keys == tuple(order[:-3]) for f in folds)
