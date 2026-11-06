"""Drift flags and the error analysis for the dashboard (docs/08 section 7.3, steps 6 and 7)."""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

DRIFT_DAYS = 7
MIN_DRIFT_GAMES = 30
MAX_BIAS = 2.0
SD_RATIO = (0.85, 1.15)
EARLY_GAMES = 10
BUCKET = 0.05


def _target(rows: pl.DataFrame, name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Actual, predicted mean and predicted spread."""
    home, away = pl.col("home_pts"), pl.col("away_pts")
    prefix = "ft"
    if name == "h1_total":
        home, away, prefix = pl.col("home_1h"), pl.col("away_1h"), "h1"
    sign = -1.0 if name == "ft_margin" else 1.0
    sd = "sd_margin" if name == "ft_margin" else "sd_total"
    frame = rows.select(
        (home + sign * away).cast(pl.Float64).alias("actual"),
        (pl.col(f"{prefix}_mu_home") + sign * pl.col(f"{prefix}_mu_away")).alias("mean"),
        pl.col(f"{prefix}_{sd}").alias("sd"),
    ).drop_nulls()
    return frame["actual"].to_numpy(), frame["mean"].to_numpy(), frame["sd"].to_numpy()


def drift(rows: pl.DataFrame, today: date) -> dict[str, Any]:
    """The last 7 days' mean error and spread against what the model said to expect."""
    if not len(rows):
        return {"games": 0, "flags": [], "targets": {}}
    recent = rows.filter(
        (pl.col("slate_date_et") >= today - timedelta(days=DRIFT_DAYS))
        & (pl.col("slate_date_et") < today)
    )
    games = len(recent)
    report: dict[str, Any] = {"games": games, "flags": [], "targets": {}}
    if games < MIN_DRIFT_GAMES:
        report["note"] = f"{games} games, {MIN_DRIFT_GAMES} needed to judge drift"
        return report
    for name in ("ft_margin", "ft_total", "h1_total"):
        actual, mean, sd = _target(recent, name)
        if not len(actual):
            continue
        errors = actual - mean
        bias = float(errors.mean())
        ratio = float(errors.std() / sd.mean())
        report["targets"][name] = {"bias": round(bias, 3), "sd_ratio": round(ratio, 3)}
        if abs(bias) > MAX_BIAS:
            report["flags"].append(f"{name} bias is {bias:+.2f} points")
        if not SD_RATIO[0] <= ratio <= SD_RATIO[1]:
            report["flags"].append(f"{name} spread ratio is {ratio:.2f}")
    return report


def _groups(frame: pl.DataFrame, key: str) -> list[dict[str, Any]]:
    grouped = (
        frame.group_by(key)
        .agg(
            pl.len().alias("games"),
            pl.col("error").mean().round(3).alias("bias"),
            pl.col("error").abs().mean().round(3).alias("mae"),
        )
        .sort("games", key, descending=[True, False])
    )
    return grouped.to_dicts()


def _rest_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= 1.5:
        return "back-to-back"
    return "2 days" if value <= 2.5 else "3+ days"


def _size_bucket(value: float) -> str:
    size = abs(value)
    if size < 5:
        return "under 5"
    return "5 to 12" if size < 12 else "12 or more"


def _labelled(frame: pl.DataFrame, source: str, label: Callable[[Any], str]) -> pl.DataFrame:
    return frame.with_columns(
        pl.col(source).map_elements(label, return_dtype=pl.String).alias("group")
    )


def error_report(rows: pl.DataFrame, scored: pl.DataFrame) -> dict[str, Any]:
    """Residuals by team, line size, favourite, venue, rest and season stage, plus hit rates."""
    report: dict[str, Any] = {"games": len(rows)}
    if len(rows):
        frame = rows.with_columns(
            (pl.col("home_pts") - pl.col("away_pts")).cast(pl.Float64).alias("actual"),
            (pl.col("ft_mu_home") - pl.col("ft_mu_away")).alias("predicted"),
        ).with_columns((pl.col("actual") - pl.col("predicted")).alias("error"))
        report["by_line_size"] = _groups(_labelled(frame, "predicted", _size_bucket), "group")
        favourite = frame.with_columns(
            pl.when(pl.col("predicted") >= 0).then(pl.lit("home favoured"))
            .otherwise(pl.lit("away favoured")).alias("group")
        )  # fmt: skip
        report["by_favourite"] = _groups(favourite, "group")
        venue = frame.with_columns(
            pl.when(pl.col("neutral")).then(pl.lit("neutral"))
            .otherwise(pl.lit("home court")).alias("group")
        )  # fmt: skip
        report["by_venue"] = _groups(venue, "group")
        if "rest_h" in frame.columns:
            report["by_rest"] = _groups(_labelled(frame, "rest_h", _rest_bucket), "group")
        if "games_h" in frame.columns:
            stage = frame.with_columns(
                pl.when(pl.col("games_h") <= EARLY_GAMES).then(pl.lit("early"))
                .otherwise(pl.lit("later")).alias("group")
            )  # fmt: skip
            report["by_season_stage"] = _groups(stage, "group")
        home = frame.select(
            pl.col("home_id").alias("team"),
            (pl.col("home_pts") - pl.col("ft_mu_home")).alias("error"),
        )
        away = frame.select(
            pl.col("away_id").alias("team"),
            (pl.col("away_pts") - pl.col("ft_mu_away")).alias("error"),
        )
        report["by_team"] = _groups(pl.concat([home, away]), "team")
    report["hit_rate"] = _hit_rate(scored)
    return report


def _hit_rate(scored: pl.DataFrame) -> list[dict[str, Any]]:
    """Per market and probability bucket, for the likelier side at the real line."""
    if not len(scored):
        return []
    settled = scored.filter(pl.col("outcome").is_not_null())
    frame = settled.with_columns(
        pl.max_horizontal(pl.col("p"), 1.0 - pl.col("p")).alias("best"),
        pl.when(pl.col("p") >= 0.5).then(pl.col("outcome")).otherwise(1.0 - pl.col("outcome"))
        .alias("hit"),
    )  # fmt: skip
    frame = frame.with_columns(
        ((pl.col("best") / BUCKET + 1e-9).floor() * BUCKET).round(2).alias("bucket")
    )
    grouped = (
        frame.group_by("family", "bucket")
        .agg(
            pl.len().alias("legs"),
            pl.col("best").mean().round(4).alias("predicted"),
            pl.col("hit").mean().round(4).alias("hit_rate"),
        )
        .sort("family", "bucket")
    )
    return grouped.to_dicts()
