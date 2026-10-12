"""Phase 6A dataset report: docs/tire_dataset.md + four sanity plots in docs/figures/tires/.

    cd backend && python -m app.tires.report [--data-dir ../data] [--hash <prefix>]

Uses the newest built dataset unless --hash is given. matplotlib is used for reports only.

Lap times are compared across circuits as the delta to each race's median clean lap. Within a
stint, tyre age and fuel_proxy move in lock-step (collinear), so the fuel effect is estimated
from the data with per-race intercepts (stints restart tyre age at different fuel loads):
    delta ~ race + b_fuel · fuel_proxy + Σ_compound (c_compound · tyre_age)
That b_fuel is descriptive only (no constant is hard-coded anywhere) and is used solely for the
"fuel-adjusted" panel. The resulting slope is a COMBINED ADJUSTED TREND (tire wear + residual
track evolution), not pure tire wear.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..config import settings  # noqa: E402
from .config import RULE_ORDER_NOTE, SEED  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "tire_dataset.md"
FIG_DIR = REPO_ROOT / "docs" / "figures" / "tires"
COMPOUND_COLORS = {"SOFT": "#d1543f", "MEDIUM": "#c98a2b", "HARD": "#8a8683"}
RULE_TEXT = {
    "lap_1_or_restart": "Lap 1 and the first lap after a red-flag restart",
    "in_or_out_lap": "PitInTime or PitOutTime present",
    "non_green_track_status": "Track-status set is not exactly {\"1\"}",
    "inaccurate_or_deleted": "IsAccurate false, Deleted true, or no LapTime",
    "wet_conditions": "Rainfall, or INTERMEDIATE / WET compound",
    "unknown_compound": "Compound missing or UNKNOWN / TEST_UNKNOWN",
    "stint_outlier": "Slower than stint median + 3 × 1.4826 × MAD (stints ≥ 5 laps)",
    "short_stint": "Stint left with < 5 clean laps",
}


def latest_outputs(out_dir: Path, prefix: str | None) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    metas = sorted(out_dir.glob("dataset_*.meta.json"), key=lambda p: p.stat().st_mtime)
    if prefix:
        metas = [m for m in metas if m.name.startswith(f"dataset_{prefix}")]
    if not metas:
        raise SystemExit(f"no dataset found in {out_dir}; run `python -m app.tires.dataset build` first")
    meta_path = metas[-1]
    short = meta_path.name[len("dataset_"):-len(".meta.json")]
    frame = pd.read_parquet(out_dir / f"dataset_{short}.parquet")
    log = json.loads((out_dir / f"exclusion_log_{short}.json").read_text(encoding="utf-8"))
    return frame, log, json.loads(meta_path.read_text(encoding="utf-8"))


def with_delta(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    df["delta_s"] = df["lap_time_s"] - df.groupby("session_key")["lap_time_s"].transform("median")
    return df


def fuel_slope(df: pd.DataFrame) -> tuple[float, dict[str, float]]:
    """OLS of delta_s on per-race intercepts + fuel_proxy + per-compound tyre_age slopes."""
    races = sorted(df["session_key"].unique())
    compounds = sorted(df["compound"].unique())
    cols = [(df["session_key"] == r).astype(float).to_numpy() for r in races]
    cols.append(df["fuel_proxy"].astype(float).to_numpy())
    cols += [((df["compound"] == c) * df["tyre_age"].astype(float)).to_numpy() for c in compounds]
    coef, *_ = np.linalg.lstsq(np.column_stack(cols), df["delta_s"].to_numpy(dtype=float), rcond=None)
    b_fuel = float(coef[len(races)])
    return b_fuel, {c: float(coef[len(races) + 1 + i]) for i, c in enumerate(compounds)}


def binned(df: pd.DataFrame, x: str, y: str, max_x: int) -> pd.DataFrame:
    d = df[df[x] <= max_x]
    g = d.groupby(x)[y]
    out = pd.DataFrame({"median": g.median(), "q25": g.quantile(0.25), "q75": g.quantile(0.75), "n": g.size()})
    return out[out["n"] >= 30]


def plots(df: pd.DataFrame, log: dict[str, Any], b_fuel: float) -> list[str]:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    df = df.assign(fuel_adj_s=df["delta_s"] - b_fuel * df["fuel_proxy"])

    # 1. lap time vs tyre age, pooled per compound: raw and fuel-adjusted
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=False)
    for ax, col, title in ((axes[0], "delta_s", "Raw (fuel still in)"),
                           (axes[1], "fuel_adj_s", f"Fuel-adjusted (b_fuel = {b_fuel:+.4f} s per lap of fuel)")):
        for compound in ("SOFT", "MEDIUM", "HARD"):
            sub = df[df["compound"] == compound]
            if sub.empty:
                continue
            b = binned(sub, "tyre_age", col, 45)
            ax.plot(b.index, b["median"], color=COMPOUND_COLORS[compound], label=f"{compound} (n={len(sub)})")
            ax.fill_between(b.index, b["q25"], b["q75"], color=COMPOUND_COLORS[compound], alpha=0.15)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("Tyre age (laps)")
        ax.set_ylabel("Lap time − race median (s)")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.suptitle("Lap time vs tyre age, pooled per compound (median, IQR band; bins with ≥ 30 laps)", fontsize=11)
    fig.tight_layout()
    names.append("lap_time_vs_tyre_age.png")
    fig.savefig(FIG_DIR / names[-1], dpi=110)
    plt.close(fig)

    # 2. stint length histogram (clean laps per kept stint)
    stints = df.groupby(["session_key", "driver", "stint"]).agg(clean=("lap_number", "size"), compound=("compound", "first"))
    fig, ax = plt.subplots(figsize=(7, 3.8))
    bins = [float(b) for b in np.arange(5, stints["clean"].max() + 3, 2)]
    ax.hist([stints.loc[stints["compound"] == c, "clean"] for c in ("SOFT", "MEDIUM", "HARD")],
            bins=bins, stacked=True, color=[COMPOUND_COLORS[c] for c in ("SOFT", "MEDIUM", "HARD")],
            label=["SOFT", "MEDIUM", "HARD"])
    ax.set_xlabel("Clean laps in stint")
    ax.set_ylabel("Stints")
    ax.set_title(f"Stint length ({len(stints)} kept stints)", fontsize=11)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    names.append("stint_length_hist.png")
    fig.savefig(FIG_DIR / names[-1], dpi=110)
    plt.close(fig)

    # 3. clean laps per race (with retention %)
    races = pd.DataFrame(log["per_race"]).T.sort_index()
    fig, ax = plt.subplots(figsize=(10, 4))
    bars = ax.bar(range(len(races)), races["kept_rows"].astype(int), color="#3b7dd8")
    for bar, ret in zip(bars, races["retention"].astype(float)):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{100 * ret:.0f}%", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(races)), [k.replace("_R", "") for k in races.index], rotation=60, fontsize=8)
    ax.set_ylabel("Clean laps kept")
    ax.set_title("Clean laps per race (label = share of all laps kept)", fontsize=11)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    names.append("clean_laps_per_race.png")
    fig.savefig(FIG_DIR / names[-1], dpi=110)
    plt.close(fig)

    # 4. fuel_proxy vs lap time
    rng = np.random.default_rng(SEED)
    sample = df.iloc[rng.choice(len(df), size=min(len(df), 6000), replace=False)]
    b = binned(df, "fuel_proxy", "delta_s", int(df["fuel_proxy"].max()))
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.scatter(sample["fuel_proxy"], sample["delta_s"], s=3, alpha=0.15, color="#8a8683", label="laps (sample)")
    ax.plot(b.index, b["median"], color="#c1553a", label="median per fuel_proxy")
    ax.set_xlabel("fuel_proxy = race laps total − lap number (laps of fuel left)")
    ax.set_ylabel("Lap time − race median (s)")
    ax.set_ylim(np.percentile(df["delta_s"], 1), np.percentile(df["delta_s"], 99))
    ax.set_title("Fuel load vs lap time (heavier → slower expected)", fontsize=11)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    names.append("fuel_proxy_vs_lap_time.png")
    fig.savefig(FIG_DIR / names[-1], dpi=110)
    plt.close(fig)
    return names


def markdown(frame: pd.DataFrame, log: dict[str, Any], meta: dict[str, Any], b_fuel: float,
             slopes: dict[str, float], figures: list[str]) -> str:
    total_in = log["input_rows"]
    lines = [
        "# Tire dataset (Phase 6A)", "",
        "Generated by `python -m app.tires.report` from the dataset built with "
        "`python -m app.tires.dataset build`. One row = one clean race lap for one driver.", "",
        "| | |", "|---|---|",
        f"| Dataset hash | `{meta['hash']}` |",
        f"| Rows (clean laps) | {meta['row_count']:,} of {total_in:,} input laps ({100 * meta['row_count'] / max(total_in, 1):.1f}%) |",
        f"| Races included | {len(meta['included_sessions'])} across seasons {', '.join(map(str, meta['seasons']))} |",
        f"| Races dropped | {len(meta['dropped_sessions'])}"
        + (" — " + "; ".join(f"{k}: {v}" for k, v in meta["dropped_sessions"].items()) if meta["dropped_sessions"] else "")
        + " |",
        f"| FastF1 | {', '.join(meta['fastf1_versions'])} · schema v{meta['schema_version']} · git `{(meta['git_sha'] or 'n/a')[:10]}` |",
        f"| Exclusion config | `{json.dumps(meta['exclusion_config'])}` |",
        "", "## Exclusions (applied in this order; each lap counted once, by the first rule that drops it)", "",
        "| # | Rule | Meaning | Dropped | Share of input |", "|---|---|---|---|---|",
    ]
    for i, rule in enumerate(log["rules"], start=1):
        n = log["totals"][rule]
        lines.append(f"| {i} | `{rule}` | {RULE_TEXT[rule]} | {n:,} | {100 * n / max(total_in, 1):.1f}% |")
    lines += [f"| | **kept** | | **{log['kept_rows']:,}** | **{100 * log['kept_rows'] / max(total_in, 1):.1f}%** |", "",
              RULE_ORDER_NOTE, "", "## Per-race retention", "",
              "| Race | Event | Input laps | Kept | Retention | Largest drop |", "|---|---|---|---|---|---|"]
    for key, race in sorted(log["per_race"].items()):
        top = max(race["drops"].items(), key=lambda kv: kv[1])
        lines.append(f"| {key} | {race['event']} | {race['input_rows']} | {race['kept_rows']} | "
                     f"{100 * race['retention']:.1f}% | {top[0]} ({top[1]}) |")
    laps_by = frame["compound"].value_counts()
    stints_by = frame.groupby(["session_key", "driver", "stint"])["compound"].first().value_counts()
    lines += ["", "## Compound distribution", "", "| Compound | Clean laps | Share | Kept stints |", "|---|---|---|---|"]
    for c, n in laps_by.items():
        lines.append(f"| {c} | {int(n):,} | {100 * n / len(frame):.1f}% | {int(stints_by.get(c, 0))} |")
    lines += ["", "## Sanity plots", ""]
    captions = {
        "lap_time_vs_tyre_age.png": "Lap time vs tyre age, pooled per compound (left raw, right fuel-adjusted).",
        "stint_length_hist.png": "Stint length (clean laps per kept stint).",
        "clean_laps_per_race.png": "Clean laps per race and retention.",
        "fuel_proxy_vs_lap_time.png": "fuel_proxy vs lap time.",
    }
    for name in figures:
        lines += [f"**{captions[name]}**", "", f"![{captions[name]}](figures/tires/{name})", ""]
    lines += ["## Descriptive fuel / tyre-age fit (sanity only, not a model)", "",
              f"OLS with per-race intercepts: `b_fuel = {b_fuel:+.4f} s` per lap of fuel_proxy "
              "(positive = heavier car is slower, as expected).", "",
              "| Compound | Combined adjusted trend (s per lap of tyre age) |", "|---|---|"]
    for c, v in sorted(slopes.items()):
        lines.append(f"| {c} | {v:+.4f} |")
    lines += ["", "These slopes are a combined adjusted trend (tire wear + residual track evolution after the "
              "fuel adjustment), not pure tire wear. Phase 6B/6C replace this with proper baselines and evaluation.",
              "", "## Notes and anomalies", "", "<!-- notes:start -->", "_To be written after reviewing the plots._",
              "<!-- notes:end -->", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tires.report")
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--hash", default=None, help="dataset hash prefix (default: newest)")
    args = parser.parse_args(argv)
    frame, log, meta = latest_outputs(Path(args.data_dir) / "tires", args.hash)
    df = with_delta(frame)
    b_fuel, slopes = fuel_slope(df)
    figures = plots(df, log, b_fuel)
    notes = ""
    if DOC.exists():  # keep hand-written review notes across regenerations
        old = DOC.read_text(encoding="utf-8")
        if "<!-- notes:start -->" in old:
            notes = old.split("<!-- notes:start -->", 1)[1].split("<!-- notes:end -->", 1)[0]
    text = markdown(frame, log, meta, b_fuel, slopes, figures)
    if notes.strip() and "_To be written" not in notes:
        text = text.replace("<!-- notes:start -->\n_To be written after reviewing the plots._\n<!-- notes:end -->",
                            f"<!-- notes:start -->{notes}<!-- notes:end -->")
    DOC.write_text(text, encoding="utf-8")
    print(f"wrote {DOC} and {len(figures)} figures in {FIG_DIR}")
    print(f"b_fuel = {b_fuel:+.4f} s/lap of fuel; tyre-age slopes: " + ", ".join(f"{k} {v:+.4f}" for k, v in sorted(slopes.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
