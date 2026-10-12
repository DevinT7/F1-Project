"""Baseline B: pooled fixed-effects OLS (statsmodels), one fit per race by default.

    lap_time = α_driver + δ_compound + β_fuel · fuel_proxy
               + Σ_c β_age,c · tyre_age [+ γ_c · tyre_age²] + β_temp · track_temp_c + ε

Deviation from the plan (documented in docs/tire_baselines.md): the plan's α is per
(driver, stint). Inside one stint tyre_age = const − fuel_proxy EXACTLY (both move one per lap),
so with an intercept per stint β_fuel and β_age are not identifiable at all (design_rank_deficit
proves it). With α per driver, the tyre-age reset at each pit stop, while fuel keeps falling,
identifies both; staggered stops across drivers identify β_age per compound. δ_compound
absorbs the compound's pace offset at age 0.

Standard errors are cluster-robust by (driver, stint). Degradation at age a for compound c is
β_age,c + 2·γ_c·a, with a 95% CI by the delta method using a t critical value with G_c − 1
degrees of freedom, where G_c = stints run on compound c, and the LARGER of the cluster-robust
and conventional standard errors (Angrist & Pischke's guard against the small-sample downward bias
of cluster-robust variance). On 1,000 synthetic races cluster-robust + z covered only 0.91 (MEDIUM);
this rule covers 0.96-0.98 for every compound with intervals ~0.005 s/lap wide.

Flags (never silent):
  confounded_fuel_age  rank-deficient design, β_fuel ≤ 0 (more fuel must be slower), or
                       |corr(tyre_age, fuel_proxy)| > 0.95
  compound_not_in_fit  the requested compound has no laps in that race
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
import patsy
import statsmodels.formula.api as smf
from scipy import stats

from .base import DegradationEstimate, FloatArray, StintContext

CORR_LIMIT = 0.95
COMPOUNDS = ("SOFT", "MEDIUM", "HARD")


@dataclass
class FixedFit:
    beta_fuel: float
    beta_age: dict[str, float]
    gamma: dict[str, float]
    flags: list[str]
    warnings: list[str]
    corr_age_fuel: float
    rank_deficit: int
    n_laps: int
    n_clusters: int
    compounds: tuple[str, ...]
    clusters_by_compound: dict[str, int]
    result: Any = field(repr=False)           # statsmodels results, cluster-robust covariance
    result_iid: Any = field(repr=False, default=None)  # same fit, conventional covariance
    temp_term: bool = False


def _formula(compounds: tuple[str, ...], *, unit: str, quadratic: bool, temp: bool) -> str:
    rhs = [f"C({unit})", "fuel_proxy", "tyre_age:C(compound)"]
    if len(compounds) > 1:
        rhs.insert(1, "C(compound)")
    if quadratic:
        rhs.append("I(tyre_age ** 2):C(compound)")
    if temp:
        rhs.append("track_temp_c")
    return "lap_time_s ~ " + " + ".join(rhs)


def _prepared(df: pd.DataFrame, pooled: bool) -> pd.DataFrame:
    d = df.copy()
    d["unit"] = (d["session_key"].astype(str) + ":" + d["driver"].astype(str)) if pooled else d["driver"].astype(str)
    d["cluster"] = d["session_key"].astype(str) + ":" + d["driver"].astype(str) + ":" + d["stint"].astype(str)
    for col in ("lap_time_s", "fuel_proxy", "tyre_age", "track_temp_c"):
        d[col] = pd.to_numeric(d[col], errors="coerce").astype(float)
    return d


def design_rank_deficit(df: pd.DataFrame, intercepts: Literal["driver", "driver_stint"] = "driver") -> int:
    """Columns minus rank of the fixed-effects design. > 0 means some effects are not identified."""
    d = _prepared(df, pooled=False)
    if intercepts == "driver_stint":
        d["unit"] = d["cluster"]
    compounds = tuple(sorted(d["compound"].unique()))
    rhs = _formula(compounds, unit="unit", quadratic=False, temp=False).split("~", 1)[1]
    x = np.asarray(patsy.dmatrix(rhs, d, return_type="dataframe"), dtype=float)
    return int(x.shape[1] - np.linalg.matrix_rank(x))


class BaselineFixed:
    name = "baseline_fixed"

    def __init__(self, *, quadratic: bool = False, pooled: bool = False) -> None:
        self.quadratic = quadratic
        self.pooled = pooled
        self.fits: dict[str, FixedFit] = {}

    # ---------------------------------------------------------------- fitting
    def fit(self, df: pd.DataFrame) -> None:
        self.fits = {}
        groups = [("pooled", df)] if self.pooled else list(df.groupby("session_key", sort=True))
        for key, group in groups:
            self.fits[str(key)] = self._fit_one(group)

    def _fit_one(self, df: pd.DataFrame) -> FixedFit:
        d = _prepared(df, self.pooled)
        compounds = tuple(c for c in COMPOUNDS if c in set(d["compound"])) or tuple(sorted(d["compound"].unique()))
        temp = bool(d["track_temp_c"].notna().all() and d["track_temp_c"].std() > 1e-6)
        formula = _formula(compounds, unit="unit", quadratic=self.quadratic, temp=temp)
        model = smf.ols(formula, data=d)
        exog = np.asarray(model.exog, dtype=float)
        deficit = int(exog.shape[1] - np.linalg.matrix_rank(exog))
        groups = pd.factorize(d["cluster"])[0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # rank-deficient designs are reported via flags instead
            result = model.fit(cov_type="cluster", cov_kwds={"groups": groups})
            result_iid = model.fit()
        params = result.params
        beta_age = {c: float(params.get(f"tyre_age:C(compound)[{c}]", np.nan)) for c in compounds}
        gamma = {c: float(params.get(f"I(tyre_age ** 2):C(compound)[{c}]", 0.0)) for c in compounds}
        beta_fuel = float(params.get("fuel_proxy", np.nan))
        age, fuel = d["tyre_age"].to_numpy(), d["fuel_proxy"].to_numpy()
        corr = float(np.corrcoef(age, fuel)[0, 1]) if age.std() > 0 and fuel.std() > 0 else 1.0

        flags: list[str] = []
        notes: list[str] = []
        if deficit > 0:
            notes.append(f"design is rank-deficient by {deficit}: fuel and tyre-age effects are not separately identified")
        if not beta_fuel > 0:
            notes.append(f"beta_fuel = {beta_fuel:+.4f} s/lap of fuel; more fuel should be slower (> 0)")
        if abs(corr) > CORR_LIMIT:
            notes.append(f"|corr(tyre_age, fuel_proxy)| = {abs(corr):.3f} > {CORR_LIMIT}")
        if notes:
            flags.append("confounded_fuel_age")
        return FixedFit(
            beta_fuel=beta_fuel, beta_age=beta_age, gamma=gamma, flags=flags, warnings=notes,
            corr_age_fuel=corr, rank_deficit=deficit, n_laps=len(d), n_clusters=int(groups.max() + 1),
            compounds=compounds,
            clusters_by_compound={c: int(d.loc[d["compound"] == c, "cluster"].nunique()) for c in compounds},
            result=result, result_iid=result_iid, temp_term=temp,
        )

    def _fit_for(self, session_key: str) -> FixedFit | None:
        return self.fits.get("pooled") if self.pooled else self.fits.get(session_key)

    # ---------------------------------------------------------------- estimates
    def degradation_per_lap(self, ctx: StintContext) -> DegradationEstimate:
        method = "baseline_fixed_quadratic" if self.quadratic else "baseline_fixed"
        fit = self._fit_for(ctx.session_key)
        if fit is None:
            return DegradationEstimate.unavailable(method, len(ctx.laps), "not_fitted")
        if ctx.compound not in fit.compounds:
            return DegradationEstimate.unavailable(method, len(ctx.laps), "compound_not_in_fit", *fit.flags)
        b_name = f"tyre_age:C(compound)[{ctx.compound}]"
        g_name = f"I(tyre_age ** 2):C(compound)[{ctx.compound}]"
        a = float(ctx.tyre_age)
        value = fit.beta_age[ctx.compound] + 2.0 * fit.gamma[ctx.compound] * a

        def delta_var(cov: pd.DataFrame) -> float:
            def cell(r: str, c: str) -> float:
                return float(np.asarray(cov.loc[[r], [c]], dtype=float)[0, 0])

            var = cell(b_name, b_name)
            if self.quadratic and g_name in cov.index:
                var += 4.0 * a * a * cell(g_name, g_name) + 4.0 * a * cell(b_name, g_name)
            return max(var, 0.0)

        variances = [delta_var(fit.result.cov_params())]
        if fit.result_iid is not None:
            variances.append(delta_var(fit.result_iid.cov_params()))
        se = float(np.sqrt(max(variances)))
        crit = float(stats.t.ppf(0.975, max(fit.clusters_by_compound[ctx.compound] - 1, 1)))
        return DegradationEstimate(value, value - crit * se, value + crit * se, method, len(ctx.laps), tuple(fit.flags))

    def predict(self, df: pd.DataFrame) -> FloatArray:
        out = np.full(len(df), np.nan)
        for key, idx, fit, d in self._predict_groups(df):
            try:
                out[idx] = np.asarray(fit.result.predict(d), dtype=float)
            except Exception:  # unseen driver / compound in this race → no prediction
                continue
        return out

    def predict_interval(self, df: pd.DataFrame, alpha: float = 0.05) -> tuple[FloatArray, FloatArray]:
        lo, hi = np.full(len(df), np.nan), np.full(len(df), np.nan)
        for _key, idx, fit, d in self._predict_groups(df):
            try:
                frame = fit.result.get_prediction(d).summary_frame(alpha=alpha)
            except Exception:
                continue
            lo[idx] = frame["obs_ci_lower"].to_numpy(dtype=float)
            hi[idx] = frame["obs_ci_upper"].to_numpy(dtype=float)
        return lo, hi

    def _predict_groups(self, df: pd.DataFrame) -> list[tuple[str, npt.NDArray[np.intp], FixedFit, pd.DataFrame]]:
        d = _prepared(df, self.pooled).reset_index(drop=True)
        keys = ["pooled"] * len(d) if self.pooled else d["session_key"].astype(str).tolist()
        out = []
        for key in sorted(set(keys)):
            fit = self.fits.get(key)
            if fit is None:
                continue
            idx = np.flatnonzero(np.asarray(keys) == key)
            out.append((key, idx, fit, d.iloc[idx]))
        return out
