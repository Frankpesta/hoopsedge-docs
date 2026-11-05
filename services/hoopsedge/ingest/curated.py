"""Curated Parquet tables: schemas, writers and the merged reader (docs/12 sections 2 and 3)."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel

from hoopsedge.common.schemas import Game, PlayerGame, TeamGame

GAME_SCHEMA: dict[str, Any] = {
    "league": pl.String,
    "season": pl.Int64,
    "game_id": pl.String,
    "tip_utc": pl.Datetime("us", "UTC"),
    "slate_date_et": pl.Date,
    "home_id": pl.String,
    "away_id": pl.String,
    "neutral": pl.Boolean,
    "venue_id": pl.String,
    "status": pl.String,
    "home_pts": pl.Int64,
    "away_pts": pl.Int64,
    "home_1h": pl.Int64,
    "away_1h": pl.Int64,
    "home_periods": pl.List(pl.Int64),
    "away_periods": pl.List(pl.Int64),
    "ot_periods": pl.Int64,
    "postseason": pl.Boolean,
    "season_flag": pl.String,
    "box_missing": pl.Boolean,
}
SCHEMAS: dict[type[BaseModel], dict[str, Any]] = {
    Game: GAME_SCHEMA,
    TeamGame: {
        **{"game_id": pl.String, "team_id": pl.String, "opp_id": pl.String, "is_home": pl.Boolean},
        **{
            name: pl.Int64
            for name in (
                "fgm",
                "fga",
                "fg3m",
                "fg3a",
                "ftm",
                "fta",
                "oreb",
                "dreb",
                "tov",
                "pf",
                "pts",
                "pts_1h",
                "minutes",
            )
        },
        "poss": pl.Float64,
    },
    PlayerGame: {
        "game_id": pl.String,
        "team_id": pl.String,
        "player_id": pl.String,
        "minutes": pl.Int64,
        "pts": pl.Int64,
        "starter": pl.Boolean,
    },
}


def write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.write_parquet(temporary)
    os.replace(temporary, path)


def rows_frame(rows: Sequence[BaseModel]) -> pl.DataFrame:
    return pl.DataFrame([row.model_dump() for row in rows], schema=SCHEMAS[type(rows[0])])


def write_table(
    data_dir: Path, table: str, league: str, season: int, rows: Sequence[BaseModel]
) -> None:
    path = data_dir / f"curated/{table}/league={league}/season={season}/part-0.parquet"
    if not rows:
        path.unlink(missing_ok=True)
        return
    write_parquet(rows_frame(rows), path)


def delta_path(data_dir: Path, table: str, day: str, league: str, season: int, game: str) -> Path:
    """One file per game, so writing the same game again replaces it instead of duplicating."""
    return data_dir / f"curated/_deltas/{table}/{day}/{league}-{season}-{game}.parquet"


def read_table(data_dir: Path, table: str, league: str, season: int) -> pl.DataFrame:
    """Read a season's table with its deltas applied. A game's delta replaces its base rows.

    `game_id` is the join key: base rows for any game with a delta are dropped wholesale, so a
    corrected box can't leave stale rows behind.
    """
    base_files = sorted(
        (data_dir / f"curated/{table}/league={league}/season={season}").glob("part-*.parquet")
    )
    delta_files = sorted(
        (data_dir / f"curated/_deltas/{table}").glob(f"*/{league}-{season}-*.parquet")
    )
    if not base_files and not delta_files:
        return pl.DataFrame()
    if not delta_files:
        return pl.concat([pl.read_parquet(file) for file in base_files])
    deltas = pl.concat([pl.read_parquet(file) for file in delta_files])
    if not base_files:
        return deltas
    base = pl.concat([pl.read_parquet(file) for file in base_files])
    return pl.concat(
        [base.join(deltas.select("game_id").unique(), on="game_id", how="anti"), deltas]
    )
