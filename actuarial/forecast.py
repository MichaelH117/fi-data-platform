"""A simple, explainable loss-ratio forecast with a rolling-origin backtest (pure numpy/pandas).

Input is the accident-quarter **ultimate** loss ratio (developed with chain-ladder, so recent quarters
are not understated). Four baselines compete; the one with the lowest backtest MAE is selected:

- ``naive``     next quarters = the last observed quarter
- ``mean_4q``   the mean of the last 4 quarters
- ``ewma``      exponentially weighted level, ``alpha = 0.3``
- ``trend_8q``  least-squares line through the last 8 quarters, extrapolated

The backtest is split by time, never randomly: for each origin it fits on quarters up to the origin
only and scores the next ``horizon`` quarters. The forecast band is +/- 1.96 x the selected model's
backtest RMSE at each step ahead, made non-decreasing over the steps (``band_rmse``): later steps have
fewer scored points, so their raw RMSE is noisy, and a band that narrowed further out would claim the
forecast gets more certain with distance.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

Model = Callable[[np.ndarray, int], np.ndarray]


def _naive(y: np.ndarray, h: int) -> np.ndarray:
    return np.repeat(y[-1], h)


def _mean_4q(y: np.ndarray, h: int) -> np.ndarray:
    return np.repeat(y[-4:].mean(), h)


def _ewma(y: np.ndarray, h: int, alpha: float = 0.3) -> np.ndarray:
    level = y[0]
    for v in y[1:]:
        level = alpha * v + (1 - alpha) * level
    return np.repeat(level, h)


def _trend_8q(y: np.ndarray, h: int) -> np.ndarray:
    w = y[-8:]
    if len(w) < 3:
        return _naive(y, h)
    x = np.arange(len(w))
    slope, intercept = np.polyfit(x, w, 1)
    return intercept + slope * (len(w) + np.arange(h))


MODELS: dict[str, Model] = {"naive": _naive, "mean_4q": _mean_4q, "ewma": _ewma, "trend_8q": _trend_8q}


def backtest(y: np.ndarray, horizon: int = 4, origins: int = 8, min_train: int = 8) -> pd.DataFrame:
    """Rolling-origin errors: one row per model x origin x step ahead (error = forecast - actual)."""
    y = np.asarray(y, dtype=float)
    rows = []
    first = max(min_train, len(y) - origins)
    for o in range(first, len(y)):  # o = number of training points; test is the next <= horizon points
        train, test = y[:o], y[o:o + horizon]
        for name, model in MODELS.items():
            f = model(train, len(test))
            for step, (fv, av) in enumerate(zip(f, test, strict=True), start=1):
                rows.append((name, o, step, fv, av, fv - av))
    return pd.DataFrame(rows, columns=["model", "origin", "step", "forecast", "actual", "error"])


def band_rmse(bt: pd.DataFrame, model: str, horizon: int) -> pd.DataFrame:
    """Per step ahead for ``model``: scored points, raw RMSE and the band RMSE (running max over steps).

    A step with no scored points carries the previous step's band forward.
    """
    g = bt[bt["model"] == model].groupby("step")["error"]
    out = pd.DataFrame({
        "points": g.size(),
        "rmse": g.apply(lambda e: float(np.sqrt((e ** 2).mean()))),
    }).reindex(range(1, horizon + 1))
    out["points"] = out["points"].fillna(0).astype(int)
    out["band_rmse"] = out["rmse"].ffill().fillna(0.0).cummax()
    return out.rename_axis("step")


def score(bt: pd.DataFrame) -> pd.DataFrame:
    """Per model: MAE, RMSE, bias and number of scored points; ``is_selected`` = lowest MAE.

    ``points_by_step`` lists the scored points at step 1, 2, ... ("8/7/6/5"): later steps have fewer.
    """
    if bt.empty:
        return pd.DataFrame(columns=["model", "points", "mae", "rmse", "bias", "is_selected", "points_by_step"])
    s = bt.groupby("model").agg(
        points=("error", "size"),
        mae=("error", lambda e: float(np.abs(e).mean())),
        rmse=("error", lambda e: float(np.sqrt((e ** 2).mean()))),
        bias=("error", "mean"),
    ).reindex(list(MODELS)).reset_index()
    s["is_selected"] = s["model"] == s.loc[s["mae"].idxmin(), "model"]
    per_step = bt.groupby(["model", "step"]).size()
    s["points_by_step"] = [
        "/".join(str(int(n)) for n in per_step.loc[m].sort_index()) if m in per_step.index.get_level_values(0) else ""
        for m in s["model"]]
    return s


def forecast(
    history: pd.Series, horizon: int = 4, origins: int = 8, min_train: int = 8
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Forecast ``history`` (indexed by quarter index, values = loss ratio) ``horizon`` quarters ahead.

    Returns (forecast table, backtest score table). The forecast table holds one row per quarter:
    history rows carry ``actual``; future rows carry every model's forecast and the selected band.
    """
    h = history.dropna().sort_index()
    y = h.to_numpy(dtype=float)
    if len(y) < 2:
        raise ValueError("need at least two quarters of history to forecast")
    bt = backtest(y, horizon, origins, min(min_train, max(len(y) - 2, 1)))
    sc = score(bt)
    selected = str(sc.loc[sc["is_selected"], "model"].iloc[0]) if len(sc) else "naive"
    steps = band_rmse(bt, selected, horizon) if len(bt) else pd.DataFrame(
        {"points": 0, "rmse": np.nan, "band_rmse": 0.0}, index=pd.RangeIndex(1, horizon + 1, name="step"))
    last = int(h.index.max())
    future = np.arange(last + 1, last + 1 + horizon)
    fut = pd.DataFrame({"q": future, "kind": "forecast", "actual": np.nan})
    for name, model in MODELS.items():
        fut[name] = model(y, horizon)
    fut["selected_model"] = selected
    fut["forecast"] = fut[selected]
    band = 1.96 * steps["band_rmse"].to_numpy()
    fut["lower"] = fut["forecast"] - band
    fut["upper"] = fut["forecast"] + band
    fut["step"] = np.arange(1, horizon + 1)
    fut["band_points"] = steps["points"].to_numpy()
    fut["step_rmse"] = steps["rmse"].to_numpy()
    fut["band_rmse"] = steps["band_rmse"].to_numpy()
    hist = pd.DataFrame({"q": h.index.astype(int), "kind": "history", "actual": y})
    out = pd.concat([hist, fut], ignore_index=True)
    out["selected_model"] = selected
    return out, sc
