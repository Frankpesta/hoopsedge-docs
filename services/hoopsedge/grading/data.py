"""Final scores for grading. Results are the point here, and nothing here reaches a feature."""

from datetime import date
from pathlib import Path

import polars as pl

from hoopsedge.grading.settle import Final
from hoopsedge.ingest.backfill import current_season
from hoopsedge.ingest.curated import read_table
from hoopsedge.pipeline.store import PipelineStore


def load_finals(data_dir: Path, league: str, today: date) -> dict[str, Final]:
    """This season's and last season's finals. IDs are unique across leagues (ESPN events)."""
    finals: dict[str, Final] = {}
    season = current_season(today)
    for year in (season - 1, season):
        frame = read_table(data_dir, "games", league, year)
        if not len(frame):
            continue
        for row in frame.iter_rows(named=True):
            finals[str(row["game_id"])] = Final(
                row["home_pts"], row["away_pts"], row["home_1h"], row["away_1h"]
            )
    return finals


def team_names(data_dir: Path) -> dict[tuple[str, str], str]:
    """Names for the hand check. D42: team IDs mean nothing without the league."""
    path = data_dir / "curated/teams/teams.parquet"
    frame = pl.read_parquet(path).select("league", "team_id", "full_name")
    return {(row[0], row[1]): row[2] for row in frame.iter_rows()}


def load_game_teams(data_dir: Path, league: str, today: date) -> dict[str, tuple[str, str]]:
    teams: dict[str, tuple[str, str]] = {}
    season = current_season(today)
    frame = read_table(data_dir, "games", league, season)
    if len(frame):
        for row in frame.select("game_id", "home_id", "away_id").iter_rows():
            teams[str(row[0])] = (str(row[1]), str(row[2]))
    return teams


def load_statuses(store: PipelineStore) -> dict[str, str]:
    """Only for games legs are waiting on, so it can tell postponed from merely missing."""
    statuses: dict[str, str] = {}
    for leg in store.pending_legs():
        item = store.game_item(str(leg["league"]), str(leg["game_id"]))
        if item is not None and item.get("status"):
            statuses[str(leg["game_id"])] = str(item["status"])
    return statuses
