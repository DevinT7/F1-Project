"""SHAP explanations for a fitted tree forecaster (Phase 6D).

TreeExplainer on the model's own encoded training rows (a seeded sample). One-hot columns
("circuit=Monza", "driver=VER", …) are summed back into their source feature so rankings compare
features, not dummy columns. Checks reported (not enforced; the report discusses them):

  ranking          mean |SHAP| per feature, descending
  tyre_age sign    Spearman correlation between tyre_age and its SHAP value (expected > 0: older
                   tyres are slower). age_gain and fuel_drop are identical in every row, so the
                   wear signal can sit on either; both are reported.
  identity share   fraction of total |SHAP| carried by driver/team (a model that is mostly
                   identity is memorising drivers, not tyres)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from .config import SEED
from .features import IDENTITY_CATEGORICAL
from .models.tree_models import TreeForecaster, fold_rows


def source_feature(column: str) -> str:
    return column.split("=", 1)[0]


@dataclass(frozen=True)
class Explanation:
    model: str
    n_rows: int
    ranking: list[tuple[str, float]]        # (feature, mean |SHAP|) descending
    sign_spearman: dict[str, float]          # feature → Spearman(feature value, SHAP value)
    identity_share: float
    figures: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "n_rows": self.n_rows, "ranking": [list(r) for r in self.ranking],
                "sign_spearman": self.sign_spearman, "identity_share": self.identity_share, "figures": self.figures}


def shap_matrix(model: TreeForecaster, X: pd.DataFrame) -> tuple[pd.DataFrame, npt.NDArray[np.float64]]:
    """(encoded rows, SHAP values with one column per encoded column)."""
    import shap

    if model.model is None or model.encoder is None:
        raise RuntimeError("fit() first")
    Xt = model.encoder.transform(X)
    values = np.asarray(shap.TreeExplainer(model.model).shap_values(Xt), dtype=np.float64)
    return Xt, values


def grouped_importance(columns: list[str], values: npt.NDArray[np.float64]) -> "pd.Series[float]":
    by_col = pd.DataFrame(values, columns=columns)
    grouped = by_col.T.groupby([source_feature(c) for c in columns]).sum().T  # sum one-hot parts per row
    return grouped.abs().mean().sort_values(ascending=False)


def explain(model: TreeForecaster, train: pd.DataFrame, *, n_rows: int = 5000,
            figure_dir: Path | None = None) -> Explanation:
    X, _, _ = fold_rows(train, model.k_step)
    X = X.sample(n=min(n_rows, len(X)), random_state=SEED).reset_index(drop=True)
    Xt, values = shap_matrix(model, X)
    columns = list(Xt.columns)
    importance = grouped_importance(columns, values)
    total = float(importance.sum()) or 1.0
    identity = float(importance[[f for f in IDENTITY_CATEGORICAL if f in importance.index]].sum())
    signs: dict[str, float] = {}
    for feat in ("tyre_age", "age_gain", "fuel_drop", "fuel_proxy"):
        if feat in columns:
            sv = values[:, columns.index(feat)]
            signs[feat] = float(pd.Series(X[feat].to_numpy(dtype=float)).corr(pd.Series(sv), method="spearman"))
    figures = plots(model.name, Xt, values, X, figure_dir) if figure_dir is not None else []
    return Explanation(model.name, len(X), [(str(k), float(v)) for k, v in importance.items()], signs,
                       identity / total, figures)


def plots(name: str, Xt: pd.DataFrame, values: npt.NDArray[np.float64], X: pd.DataFrame, out: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap

    out.mkdir(parents=True, exist_ok=True)
    numeric = [c for c in Xt.columns if "=" not in c and str(Xt[c].dtype) != "category"]
    idx = [list(Xt.columns).index(c) for c in numeric]
    paths = []
    shap.summary_plot(values[:, idx], Xt[numeric].astype(float), show=False, max_display=15)
    plt.title(f"SHAP summary — {name} (numeric features)")
    path = out / "shap_summary.png"
    plt.tight_layout()
    plt.savefig(path, dpi=110)
    plt.close("all")
    paths.append(path.name)
    for feat in ("tyre_age", "age_gain", "steps_ahead"):
        if feat not in numeric:
            continue
        j = list(Xt.columns).index(feat)
        fig, ax = plt.subplots(figsize=(6, 4))
        sc = ax.scatter(X[feat], values[:, j], c=X["compound_ord"], s=4, alpha=0.4, cmap="viridis")
        fig.colorbar(sc, ax=ax, label="compound (0 soft, 1 medium, 2 hard)")
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_xlabel(feat)
        ax.set_ylabel(f"SHAP value for {feat} (s)")
        ax.set_title(f"SHAP dependence — {feat}")
        fig.tight_layout()
        path = out / f"shap_dependence_{feat}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        paths.append(path.name)
    return paths
