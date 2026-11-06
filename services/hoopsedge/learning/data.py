"""What was predicted and offered, joined with what happened (docs/12 `predictions_log`).

Results are read here to score earlier predictions, and never reach a feature. These tables are
how calibrators finally get to see real SportyBet lines (D72).
"""

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.engine import Prediction, ScoredLine
from hoopsedge.grading.settle import LOST, PUSH, WON, Final, settle_leg
from hoopsedge.ingest.backfill import FIRST_SEASON, current_season
from hoopsedge.ingest.curated import read_table, write_parquet

SCORED = "scored_offers"
PREDICTIONS = "predictions_log"
PERIOD_FIELDS = ("mu_home", "mu_away", "sd_margin", "sd_total", "sd_home", "sd_away")


def _path(data_dir: Path, table: str, league: str, slate: date, key: str) -> Path:
    return data_dir / f"curated/{table}/league={league}/slate={slate}/{key}.parquet"


def has_log(data_dir: Path, league: str, slate: date, key: str) -> bool:
    return _path(data_dir, SCORED, league, slate, key).exists()


def _prediction_row(item: Prediction, slate: date, key: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "game_id": item.game_id,
        "slate_date_et": slate,
        "source": key,
        "model_version": item.model_version,
        "feature_hash": item.feature_hash,
    }
    for name, period in (("ft", item.ft), ("h1", item.h1)):
        for field in PERIOD_FIELDS:
            row[f"{name}_{field}"] = None if period is None else float(getattr(period, field))
    return row


def write_log(
    data_dir: Path,
    league: str,
    slate: date,
    key: str,
    predictions: Sequence[Prediction],
    scored: Sequence[ScoredLine],
) -> None:
    """One file per run. Writing the same key again replaces it, so a re-run can't duplicate."""
    if predictions:
        frame = pl.DataFrame([_prediction_row(item, slate, key) for item in predictions])
        write_parquet(frame, _path(data_dir, PREDICTIONS, league, slate, key))
    if scored:
        rows = [{**item.model_dump(), "slate_date_et": slate, "source": key} for item in scored]
        write_parquet(pl.DataFrame(rows), _path(data_dir, SCORED, league, slate, key))


def _read_all(data_dir: Path, table: str, league: str) -> pl.DataFrame:
    files = sorted((data_dir / f"curated/{table}/league={league}").glob("slate=*/*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(file) for file in files], how="diagonal_relaxed")


def finals_frame(data_dir: Path, league: str, today: date) -> pl.DataFrame:
    frames = [
        read_table(data_dir, "games", league, season)
        for season in range(FIRST_SEASON[league], current_season(today) + 1)
    ]
    return pl.concat([frame for frame in frames if len(frame)])


def scored_rows(data_dir: Path, league: str, today: date) -> pl.DataFrame:
    """One row per game and market: the last line seen, its raw chance and how it settled.

    `p` is the push-free chance of home or over, the scale calibrators work on (D70). `outcome` is
    null on a push, which settles nothing.
    """
    logged = _read_all(data_dir, SCORED, league)
    if not len(logged):
        return pl.DataFrame()
    latest = logged.sort("fetched_at").unique(
        ["game_id", "family", "team"], keep="last", maintain_order=True
    )
    finals = finals_frame(data_dir, league, today).select(
        "game_id", "home_pts", "away_pts", "home_1h", "away_1h"
    )
    joined = latest.join(finals, on="game_id")
    outcomes: list[float | None] = []
    for row in joined.iter_rows(named=True):
        side = "home" if row["family"] == "handicap" else "over"
        final = Final(row["home_pts"], row["away_pts"], row["home_1h"], row["away_1h"])
        try:
            status = settle_leg(row["family"], row["line"], row["team"], side, final).status
        except ValueError:
            outcomes.append(None)
            continue
        outcomes.append({WON: 1.0, LOST: 0.0, PUSH: None}[status])
    return joined.with_columns(
        pl.Series("outcome", outcomes, dtype=pl.Float64),
        (pl.col("p_above") / (1.0 - pl.col("p_push"))).alias("p"),
    )


def prediction_rows(data_dir: Path, league: str, today: date) -> pl.DataFrame:
    """The latest prediction per game, with the result and the game's context."""
    logged = _read_all(data_dir, PREDICTIONS, league)
    if not len(logged):
        return pl.DataFrame()
    latest = logged.unique("game_id", keep="last", maintain_order=True)
    finals = finals_frame(data_dir, league, today).select(
        "game_id", "home_id", "away_id", "neutral", "home_pts", "away_pts", "home_1h", "away_1h"
    )
    return latest.join(finals, on="game_id")
