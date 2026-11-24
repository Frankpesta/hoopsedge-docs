"""Curated v2 tables to the model's plain inputs. The model itself never reads files."""

from pathlib import Path

import polars as pl

from hoopsedge.ingest.scores import read_results, read_teams
from hoopsedge.l5.model import Result


def completed_results(frame: pl.DataFrame) -> list[Result]:
    """Finished games only. A scheduled game shows 0-0 at ESPN and must never count."""
    finals = frame.filter(pl.col("completed"))
    if finals.select(pl.col("home_pts").is_null() | pl.col("away_pts").is_null()).to_series().any():
        raise ValueError("A completed result has no score")
    return [
        Result(
            season=row["season"],
            game_id=row["game_id"],
            kickoff_utc=row["kickoff_utc"],
            home_id=row["home_id"],
            away_id=row["away_id"],
            home_pts=row["home_pts"],
            away_pts=row["away_pts"],
        )
        for row in finals.iter_rows(named=True)
    ]


def season_inputs(
    data_dir: Path, sport: str, season: int
) -> tuple[pl.DataFrame, list[Result], dict[str, str]]:
    """The season's result rows, its finals as model inputs, and team levels."""
    results = read_results(data_dir, sport, season)
    teams = read_teams(data_dir, sport, season)
    levels = dict(zip(teams["team_id"].to_list(), teams["level"].to_list(), strict=True))
    return results, completed_results(results), levels
