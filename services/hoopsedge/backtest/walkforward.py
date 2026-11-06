"""Walk-forward prediction: each window is predicted by models fit only on what was known then.

Features already follow D14. Training rows follow it too: a game trains a model only if its result
was known at the slate's run time on the window's first day (`tip + 3 h < run time`).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from hoopsedge.features.training import slate_run_time
from hoopsedge.models.baselines import elo_baseline, prior_baseline, ratings_baseline, targets_of
from hoopsedge.models.distribution import ScoreDistribution
from hoopsedge.models.training import PERIODS, fit_bundle
from hoopsedge.ratings.history import RESULT_KNOWN_AFTER

PREDICTION_COLUMNS = {
    "game_id": pl.String,
    "league": pl.String,
    "season": pl.Int64,
    "slate_date_et": pl.Date,
    "model": pl.String,
    "period": pl.String,
    "mu_home": pl.Float64,
    "mu_away": pl.Float64,
    "sd_margin": pl.Float64,
    "sd_total": pl.Float64,
    "sd_home": pl.Float64,
    "sd_away": pl.Float64,
    "actual_home": pl.Float64,
    "actual_away": pl.Float64,
    "games_home": pl.Float64,
    "games_away": pl.Float64,
    "days_since_start": pl.Float64,
    "prior_weight_home": pl.Float64,
    "prior_weight_away": pl.Float64,
}


@dataclass(frozen=True)
class Window:
    """Slates in `[start, end)`, predicted together by models fit before `start`."""

    start: date
    end: date


def season_windows(first: date, last: date, refit_days: int) -> list[Window]:
    """Consecutive windows from `first` through `last`, refitting every `refit_days` days."""
    windows: list[Window] = []
    start = first
    stop = last + timedelta(days=1)
    while start < stop:
        end = min(start + timedelta(days=refit_days), stop)
        windows.append(Window(start, end))
        start = end
    return windows


def known_before(frame: pl.DataFrame, league: str, slate: date) -> pl.DataFrame:
    """Rows whose results were known when the run for `slate` happened (D14)."""
    cutoff = slate_run_time(league, slate) - RESULT_KNOWN_AFTER
    return frame.filter(pl.col("tip_utc") < cutoff)


def _rows(
    rows: pl.DataFrame, league: str, model: str, period: str, dist: ScoreDistribution
) -> pl.DataFrame:
    home, away = targets_of(rows, period)
    return pl.DataFrame(
        {
            "game_id": rows["game_id"],
            "league": [league] * len(rows),
            "season": rows["season"],
            "slate_date_et": rows["slate_date_et"],
            "model": [model] * len(rows),
            "period": [period] * len(rows),
            "mu_home": dist.mu_home,
            "mu_away": dist.mu_away,
            "sd_margin": dist.sd_margin,
            "sd_total": dist.sd_total,
            "sd_home": dist.sd_home,
            "sd_away": dist.sd_away,
            "actual_home": home,
            "actual_away": away,
            "games_home": rows["games_h"].cast(pl.Float64),
            "games_away": rows["games_a"].cast(pl.Float64),
            "days_since_start": rows["days_since_start"].cast(pl.Float64),
            "prior_weight_home": rows["prior_weight_h"].cast(pl.Float64),
            "prior_weight_away": rows["prior_weight_a"].cast(pl.Float64),
        },
        schema=PREDICTION_COLUMNS,
    ).with_columns(pl.col(pl.Float64).fill_nan(None))


def predict_window(
    frame: pl.DataFrame, window: Window, params: dict[str, Any], league: str
) -> pl.DataFrame:
    train = known_before(frame, league, window.start)
    rows = frame.filter(
        (pl.col("slate_date_et") >= window.start) & (pl.col("slate_date_et") < window.end)
    )
    if train.is_empty():
        raise ValueError(f"No training rows before {window.start}")
    if rows.is_empty():
        return pl.DataFrame(schema=PREDICTION_COLUMNS)
    bundle = fit_bundle(train, params)
    parts = []
    for period in PERIODS:
        parts.append(_rows(rows, league, "model", period, bundle.predict(rows, period)))
        parts.append(_rows(rows, league, "baseline", period, ratings_baseline(train, rows, period)))
        parts.append(_rows(rows, league, "elo", period, elo_baseline(train, rows, period)))
        prior = train.filter(pl.col(f"prior_home_{period}").is_not_null())
        if not prior.is_empty():
            parts.append(_rows(rows, league, "prior", period, prior_baseline(prior, rows, period)))
    return pl.concat(parts)


def run_walkforward(
    frame: pl.DataFrame, windows: list[Window], params: dict[str, Any], league: str
) -> pl.DataFrame:
    parts = [predict_window(frame, window, params, league) for window in windows]
    return pl.concat(parts).filter(pl.col("mu_home").is_not_null())


def margin_errors(predictions: pl.DataFrame, period: str = "ft") -> dict[str, float]:
    """A quick summary for logs: mean absolute margin error by candidate."""
    result: dict[str, float] = {}
    usable = predictions.filter(pl.col("period") == period)
    for (model,), group in usable.group_by(["model"]):
        actual = (group["actual_home"] - group["actual_away"]).to_numpy()
        predicted = (group["mu_home"] - group["mu_away"]).to_numpy()
        result[str(model)] = float(np.nanmean(np.abs(actual - predicted)))
    return result
