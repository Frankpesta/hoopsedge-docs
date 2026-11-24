"""Loads the training table: features, targets, season flags and prior-only inputs."""

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from hoopsedge.features.registry import FEATURE_SET_VERSION
from hoopsedge.ingest.backfill import FIRST_SEASON, current_season
from hoopsedge.ingest.curated import read_table
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.models.baselines import prior_only_columns
from hoopsedge.ratings.engine import Engine
from hoopsedge.ratings.history import History


def load_games(data_dir: Path, league: str) -> pl.DataFrame:
    """Every curated game from the league's first kept season on, deltas included."""
    today = datetime.now(UTC).astimezone(EASTERN).date()
    seasons = range(FIRST_SEASON[league], current_season(today) + 1)
    frames = [read_table(data_dir, "games", league, season) for season in seasons]
    return pl.concat([frame for frame in frames if len(frame)]).sort(["tip_utc", "game_id"])


def load_frame(data_dir: Path, league: str) -> pl.DataFrame:
    """One row per final game with features at the current version, oldest first."""
    folder = data_dir / f"curated/features/league={league}"
    files = sorted(folder.glob("season=*.parquet"))
    if not files:
        raise FileNotFoundError(f"No features for {league}: run hoopsedge.features.training build")
    features = pl.concat([pl.read_parquet(path) for path in files])
    versions = features["feature_set_version"].unique().to_list()
    if versions != [FEATURE_SET_VERSION]:
        raise ValueError(f"Features at {versions}, expected only {FEATURE_SET_VERSION}: rebuild")
    if features["game_id"].n_unique() != len(features):
        raise ValueError(f"Duplicate game IDs in the {league} features")
    flags = load_games(data_dir, league).select("game_id", "season_flag")
    seasons = sorted(features["season"].unique().to_list())
    engine = Engine(History.load(data_dir, league, seasons))
    priors = pl.concat([prior_only_columns(engine, season) for season in seasons])
    return (
        features.join(flags, on="game_id", how="left")
        .join(priors, on="game_id", how="left")
        .with_columns(pl.col(pl.Float64).fill_nan(None))
        .sort(["tip_utc", "game_id"])
    )


def predictions_path(data_dir: Path, league: str) -> Path:
    return data_dir / f"backtest/{league}/predictions.parquet"


def load_predictions(data_dir: Path, league: str) -> pl.DataFrame:
    return pl.read_parquet(predictions_path(data_dir, league))


def load_lines(data_dir: Path, league: str) -> pl.DataFrame:
    folder = data_dir / f"curated/lines/league={league}"
    files = sorted(folder.glob("season=*.parquet"))
    if not files:
        raise FileNotFoundError(f"No lines for {league}: run hoopsedge.backtest.run lines")
    return pl.concat([pl.read_parquet(path) for path in files])
