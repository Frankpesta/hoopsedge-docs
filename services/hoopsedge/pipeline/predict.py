"""Live predictions (architecture step 6). Training and live share `features_for_game` (D56)."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.engine.schemas import (
    Confidence,
    InjuryStatus,
    PeriodPrediction,
    Prediction,
)
from hoopsedge.features.builder import (
    AVAILABILITY,
    MAX_AGE,
    START_RATE_FOR_STARTER,
    STATUS_WEIGHTS,
    features_for_game,
)
from hoopsedge.features.registry import FEATURE_SET_VERSION, FEATURES
from hoopsedge.ingest.backfill import FIRST_SEASON
from hoopsedge.models.distribution import ScoreDistribution
from hoopsedge.models.registry import load_bundle, load_calibrators
from hoopsedge.ratings.engine import Engine
from hoopsedge.ratings.history import GameRef, History

UNCERTAIN_STATUSES = frozenset(AVAILABILITY["uncertain_statuses"])
REQUIRED = ("game_id", "season", "tip_utc", "home_id", "away_id", "slate_date_et")


def latest_version(models_root: Path, league: str) -> Path:
    """The newest model version. Phase 7's champion record replaces this."""
    folders = sorted(path for path in (models_root / league).glob("*") if path.is_dir())
    if not folders:
        raise FileNotFoundError(f"No model versions for {league} in {models_root}")
    return folders[-1]


def history_seasons(league: str, season: int) -> list[int]:
    """Exactly the span training replays. Each season's priors come from the one before it, so a
    shorter span gives different ratings, and the model would see features unlike its training.
    """
    return list(range(FIRST_SEASON[league], season + 1))


def game_ref(league: str, item: Mapping[str, Any]) -> GameRef | None:
    """None if the item doesn't yet say everything a prediction needs, such as neutral."""
    if any(item.get(name) is None for name in (*REQUIRED, "neutral", "postseason")):
        return None
    return GameRef(
        league=league,
        season=int(item["season"]),
        game_id=str(item["game_id"]),
        tip_utc=datetime.fromisoformat(str(item["tip_utc"])),
        home_id=str(item["home_id"]),
        away_id=str(item["away_id"]),
        neutral=bool(item["neutral"]),
        postseason=bool(item["postseason"]),
        slate_date_et=date.fromisoformat(str(item["slate_date_et"])),
    )


def injury_status(history: History, game: GameRef, as_of: datetime) -> InjuryStatus | None:
    """D71: is a regular starter on either team uncertain? None means no recent snapshot.

    Out isn't uncertain: it's known. A status we've never seen counts as uncertain (D29).
    """
    snapshot = history.latest_injuries(as_of, MAX_AGE)
    if snapshot is None:
        return None
    questionable = False
    for team in (game.home_id, game.away_id):
        minutes = history.player_minutes(game.season, team, as_of)
        if not len(minutes):
            continue
        starters = set(
            minutes.filter(pl.col("start_rate") > START_RATE_FOR_STARTER)["player_id"].to_list()
        )
        for row in snapshot.filter(pl.col("team_id") == team).to_dicts():
            uncertain = row["status"] in UNCERTAIN_STATUSES or row["status"] not in STATUS_WEIGHTS
            questionable = questionable or (uncertain and row["player_id"] in starters)
    taken: datetime = snapshot["captured_at"].max()  # type: ignore[assignment]
    return InjuryStatus(questionable_starter=questionable, snapshot_at=taken)


def _period(dist: ScoreDistribution, row: int) -> PeriodPrediction:
    return PeriodPrediction(
        mu_home=float(dist.mu_home[row]),
        mu_away=float(dist.mu_away[row]),
        sd_margin=float(dist.sd_margin[row]),
        sd_total=float(dist.sd_total[row]),
        sd_home=float(dist.sd_home[row]),
        sd_away=float(dist.sd_away[row]),
    )


def predict_games(
    data_dir: Path, league: str, games: Sequence[GameRef], now: datetime, model_folder: Path
) -> list[Prediction]:
    if not games:
        return []
    built_for = (model_folder / "feature_set_version.txt").read_text("utf-8").strip()
    if built_for != FEATURE_SET_VERSION:
        raise ValueError(f"Model built for features {built_for}, now {FEATURE_SET_VERSION}")
    season = max(game.season for game in games)
    history = History.load(data_dir, league, history_seasons(league, season))
    engine = Engine(history)
    bundle = load_bundle(model_folder)
    rows = [(game, features_for_game(engine, game, min(now, game.tip_utc))) for game in games]
    frame = pl.DataFrame(
        [row.features for _, row in rows], schema=dict.fromkeys(FEATURES, pl.Float64)
    ).with_columns(pl.col(pl.Float64).fill_nan(None))
    full = bundle.predict(frame, "ft")
    half = bundle.predict(frame, "h1")
    predictions: list[Prediction] = []
    for number, (game, row) in enumerate(rows):
        as_of = min(now, game.tip_utc)
        features = row.features
        predictions.append(
            Prediction(
                game_id=game.game_id,
                league=game.league,  # type: ignore[arg-type]
                model_version=model_folder.name,
                feature_hash=row.feature_hash,
                ft=_period(full, number),
                h1=_period(half, number),
                confidence=Confidence(
                    games_home=int(features["games_h"] or 0),
                    games_away=int(features["games_a"] or 0),
                    prior_weight_home=float(features["prior_weight_h"] or 0.0),
                    prior_weight_away=float(features["prior_weight_a"] or 0.0),
                ),
                injury=injury_status(history, game, as_of) if league == "NBA" else None,
            )
        )
    return predictions


def load_model_calibrators(model_folder: Path, league: str) -> dict[str, dict[str, Any]]:
    return {league: load_calibrators(model_folder)}
