"""Turn predictions into market probabilities on one shared line set (D17, D18).

Real market lines exist only for handicap and full-game total (D27). Every family also gets
synthetic lines at the ratings baseline's median plus offsets, the same for every candidate.
Probabilities here are the push-free ranking kind, and pushes have a null outcome (no bet settled).
"""

from datetime import date
from typing import Any

import numpy as np
import polars as pl

from hoopsedge.models.calibration import MIN_SAMPLES, Calibrator
from hoopsedge.models.distribution import line_probabilities, ranking_probability

MODEL = "model"
BASELINE = "baseline"
REAL = "real"
SYNTHETIC = "synthetic"
FAMILIES = ("handicap", "total_ft", "total_1h", "team_total")
OFFSETS = (-5.0, -2.5, 0.0, 2.5, 5.0)

SCORED_SCHEMA: dict[str, Any] = {
    "game_id": pl.String,
    "league": pl.String,
    "season": pl.Int64,
    "slate_date_et": pl.Date,
    "model": pl.String,
    "family": pl.String,
    "source": pl.String,
    "selection": pl.String,
    "line": pl.Float64,
    "p": pl.Float64,
    "outcome": pl.Float64,
    "price": pl.Float64,
    "price_other": pl.Float64,
    "games_home": pl.Float64,
    "games_away": pl.Float64,
    "days_since_start": pl.Float64,
}


def _half_point(values: np.ndarray) -> np.ndarray:
    return np.round(values * 2.0) / 2.0


def _settled(actual: np.ndarray, line: np.ndarray) -> np.ndarray:
    """1 above the line, 0 below it, NaN on it or with no score."""
    result = np.where(actual > line, 1.0, 0.0)
    return np.where((actual == line) | ~np.isfinite(actual), np.nan, result)


def _probabilities(mu: np.ndarray, sd: np.ndarray, line: np.ndarray) -> np.ndarray:
    above, push, _ = line_probabilities(mu, sd, line)
    return ranking_probability(above, push)


def _rows(
    base: pl.DataFrame,
    family: str,
    source: str,
    selection: str,
    line: np.ndarray,
    p: np.ndarray,
    outcome: np.ndarray,
    price: np.ndarray | None = None,
    price_other: np.ndarray | None = None,
) -> pl.DataFrame:
    count = len(base)
    default = np.full(count, np.nan)
    return base.select(
        ["game_id", "league", "season", "slate_date_et", "model", "games_home", "games_away",
         "days_since_start"]
    ).with_columns(
        pl.lit(family).alias("family"),
        pl.lit(source).alias("source"),
        pl.lit(selection).alias("selection"),
        pl.Series("line", line, dtype=pl.Float64),
        pl.Series("p", p, dtype=pl.Float64),
        pl.Series("outcome", outcome, dtype=pl.Float64).fill_nan(None),
        pl.Series("price", default if price is None else price, dtype=pl.Float64).fill_nan(None),
        pl.Series(
            "price_other", default if price_other is None else price_other, dtype=pl.Float64
        ).fill_nan(None),
    )  # fmt: skip


def _column(frame: pl.DataFrame, name: str) -> np.ndarray:
    return frame[name].cast(pl.Float64).fill_null(np.nan).to_numpy()


def _with_centres(period: pl.DataFrame) -> pl.DataFrame:
    """Each candidate's rows gain the baseline's medians, which set the shared synthetic lines."""
    baseline = period.filter(pl.col("model") == BASELINE).select(
        "game_id",
        (pl.col("mu_home") - pl.col("mu_away")).alias("centre_margin"),
        (pl.col("mu_home") + pl.col("mu_away")).alias("centre_total"),
        pl.col("mu_home").alias("centre_home"),
        pl.col("mu_away").alias("centre_away"),
    )
    return period.join(baseline, on="game_id", how="inner")


def _synthetic(period: pl.DataFrame, name: str) -> list[pl.DataFrame]:
    rows = _with_centres(period)
    mu_home, mu_away = _column(rows, "mu_home"), _column(rows, "mu_away")
    actual_home, actual_away = _column(rows, "actual_home"), _column(rows, "actual_away")
    parts: list[pl.DataFrame] = []
    for offset in OFFSETS:
        if name == "ft":
            # Line on the margin. In handicap terms, home_line = -margin_line.
            margin_line = _half_point(_column(rows, "centre_margin")) + offset
            parts.append(
                _rows(
                    rows,
                    "handicap",
                    SYNTHETIC,
                    "home",
                    -margin_line,
                    _probabilities(mu_home - mu_away, _column(rows, "sd_margin"), margin_line),
                    _settled(actual_home - actual_away, margin_line),
                )  # fmt: skip
            )
        family = "total_ft" if name == "ft" else "total_1h"
        total_line = _half_point(_column(rows, "centre_total")) + offset
        parts.append(
            _rows(
                rows,
                family,
                SYNTHETIC,
                "over",
                total_line,
                _probabilities(mu_home + mu_away, _column(rows, "sd_total"), total_line),
                _settled(actual_home + actual_away, total_line),
            )  # fmt: skip
        )
        if name == "ft":
            for side, mu, sd, actual, centre in (
                ("home_over", mu_home, "sd_home", actual_home, "centre_home"),
                ("away_over", mu_away, "sd_away", actual_away, "centre_away"),
            ):
                line = _half_point(_column(rows, centre)) + offset / 2.0
                parts.append(
                    _rows(
                        rows,
                        "team_total",
                        SYNTHETIC,
                        side,
                        line,
                        _probabilities(mu, _column(rows, sd), line),
                        _settled(actual, line),
                    )  # fmt: skip
                )
    return parts


def _real(period: pl.DataFrame, lines: pl.DataFrame) -> list[pl.DataFrame]:
    rows = period.join(lines, on="game_id", how="inner")
    if rows.is_empty():
        return []
    mu_home, mu_away = _column(rows, "mu_home"), _column(rows, "mu_away")
    actual_home, actual_away = _column(rows, "actual_home"), _column(rows, "actual_away")
    spread = _column(rows, "spread_home")
    total = _column(rows, "total")
    return [
        _rows(
            rows, "handicap", REAL, "home", spread,
            _probabilities(mu_home - mu_away, _column(rows, "sd_margin"), -spread),
            _settled(actual_home - actual_away, -spread),
            _column(rows, "price_home"), _column(rows, "price_away"),
        ),
        _rows(
            rows, "total_ft", REAL, "over", total,
            _probabilities(mu_home + mu_away, _column(rows, "sd_total"), total),
            _settled(actual_home + actual_away, total),
            _column(rows, "price_over"), _column(rows, "price_under"),
        ),
    ]  # fmt: skip


def score_markets(predictions: pl.DataFrame, lines: pl.DataFrame) -> pl.DataFrame:
    """One row per candidate, game, family, line and selection, on identical lines for all."""
    parts: list[pl.DataFrame] = []
    full = predictions.filter(pl.col("period") == "ft")
    half = predictions.filter(pl.col("period") == "h1")
    parts += _synthetic(full, "ft")
    parts += _synthetic(half, "h1")
    parts += _real(full, lines)
    scored = pl.concat(parts).select(list(SCORED_SCHEMA))
    return scored.filter(pl.col("p").is_finite())


def with_calibration(scored: pl.DataFrame, split: date, end: date | None = None) -> pl.DataFrame:
    """Calibrate rows in `[split, end)`, each candidate and family on its own earlier rows.

    A group with too few earlier samples stays uncalibrated, and `calibrated` says which those
    were, so they can be left out of any calibrated claim.
    """
    earlier = scored.filter(pl.col("slate_date_et") < split)
    later = scored.filter(pl.col("slate_date_et") >= split)
    if end is not None:
        later = later.filter(pl.col("slate_date_et") < end)
    if later.is_empty():
        return later.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("p_calibrated"),
            pl.lit(None, dtype=pl.Boolean).alias("calibrated"),
        )
    parts: list[pl.DataFrame] = []
    for keys, group in later.group_by(["model", "family", "source"], maintain_order=True):
        model, family, source = keys
        fit = earlier.filter(
            (pl.col("model") == model)
            & (pl.col("family") == family)
            & (pl.col("source") == source)
            & pl.col("outcome").is_not_null()
        )
        raw = group["p"].to_numpy()
        calibrated = raw
        applied = False
        if len(fit) >= MIN_SAMPLES and fit["outcome"].n_unique() == 2:
            calibrator = Calibrator.fit(fit["p"].to_numpy(), fit["outcome"].to_numpy())
            calibrated = calibrator.apply(raw)
            applied = True
        parts.append(
            group.with_columns(
                pl.Series("p_calibrated", calibrated, dtype=pl.Float64),
                pl.lit(applied).alias("calibrated"),
            )
        )
    return pl.concat(parts)
