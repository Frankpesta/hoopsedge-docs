"""The feature vector for one game at one moment. Training and live both call this."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from hoopsedge.features.registry import FACTORS, FEATURE_SET_VERSION, FEATURES
from hoopsedge.ratings.engine import CONFIG, Engine, RatingsSnapshot
from hoopsedge.ratings.history import GameRef, to_micros

REST_CAP_DAYS = 7.0
BACK_TO_BACK_DAYS = 1.5
WEEK_MICROS = 7 * 86_400_000_000
DAY_MICROS = 86_400_000_000
ROUND_PLACES = 6
SATURDAY = 5
START_RATE_FOR_STARTER = 0.5
AVAILABILITY = CONFIG["availability"]
MAX_AGE = timedelta(hours=float(AVAILABILITY["max_snapshot_age_hours"]))
STATUS_WEIGHTS: dict[str, float] = AVAILABILITY["status_weights"]


@dataclass(frozen=True)
class FeatureRow:
    game_id: str
    as_of: datetime
    feature_set_version: str
    feature_hash: str
    features: dict[str, float | None]


def hash_features(features: dict[str, float | None]) -> str:
    """SHA-256 of canonical JSON with floats at 6 places (docs/12 section 4)."""
    rounded = {
        name: None if value is None else round(float(value), ROUND_PLACES)
        for name, value in features.items()
    }
    canonical = json.dumps(rounded, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schedule_features(
    engine: Engine, season: int, team: str, tip: datetime, as_of: datetime
) -> dict[str, float | None]:
    """Rest and load, from games that tipped before this one and before `as_of`."""
    tips = engine.history.season(season).played_before(team, min(tip, as_of))
    tip_micros = to_micros(tip)
    if not len(tips):
        return {"rest": None, "b2b": 0.0, "games_7d": 0.0}
    rest = (tip_micros - int(tips[-1])) / DAY_MICROS
    return {
        "rest": min(rest, REST_CAP_DAYS),
        "b2b": 1.0 if rest <= BACK_TO_BACK_DAYS else 0.0,
        "games_7d": float(np.sum(tips >= tip_micros - WEEK_MICROS)),
    }


def _availability(
    engine: Engine, season: int, team: str, as_of: datetime
) -> tuple[float | None, float | None]:
    """Share of minutes missing, overall and among starters. None when it isn't known (D15)."""
    if engine.history.league != "NBA":
        return None, None
    snapshot = engine.history.latest_injuries(as_of, MAX_AGE)
    if snapshot is None:
        return None, None
    minutes = engine.history.player_minutes(season, team, as_of)
    if not len(minutes):
        return None, None
    total = float(minutes["minutes"].sum())
    if total <= 0:
        return None, None
    missing = {
        row["player_id"]: row["status"] for row in snapshot.to_dicts() if row["team_id"] == team
    }
    if any(status not in STATUS_WEIGHTS for status in missing.values()):
        return None, None  # An unrecognized status gets no guess (D29).
    lost = starters = 0.0
    for row in minutes.to_dicts():
        status = missing.get(row["player_id"])
        if status is None:
            continue
        share = STATUS_WEIGHTS[status] * row["minutes"] / total
        lost += share
        if row["start_rate"] > START_RATE_FOR_STARTER:
            starters += share
    return lost, starters


def _team_block(snap: RatingsSnapshot, team: str, side: str) -> dict[str, float | None]:
    block: dict[str, float | None] = {
        f"adj_o_{side}": snap.league["mu"] + snap.value("off", team),
        f"adj_d_{side}": snap.league["mu"] - snap.value("def", team),
        f"adj_t_{side}": snap.league["tau"] / 2 + snap.value("tempo", team),
        f"elo_{side}": snap.value("elo", team),
        f"games_{side}": float(snap.games_played(team)),
        f"prior_weight_{side}": snap.prior_weight(team),
        f"volatility_{side}": snap.volatility_of(team),
        f"form_{side}": snap.form_of(team),
        f"travel_km_{side}": None,  # No venue coordinates yet, which docs/04 allows.
    }
    for factor in FACTORS:
        level = snap.league[f"mu_{factor}"]
        block[f"{factor}_o_{side}"] = level + snap.value(f"{factor}_o", team)
        block[f"{factor}_d_{side}"] = level - snap.value(f"{factor}_d", team)
    return block


def features_for_game(engine: Engine, game: GameRef, as_of: datetime) -> FeatureRow:
    """Everything knowable about `game` at `as_of`, which can't be later than tip-off."""
    if as_of > game.tip_utc:
        raise ValueError(f"as_of {as_of} is after tip {game.tip_utc} for {game.game_id}")
    snap = engine.ratings_as_of(game.season, as_of)
    home = engine.history.pooled_team(game.season, game.home_id)
    away = engine.history.pooled_team(game.season, game.away_id)
    league = snap.league
    edge = 0.0 if game.neutral else league["hca"] / 2

    features: dict[str, float | None] = {
        **_team_block(snap, home, "h"),
        **_team_block(snap, away, "a"),
        "elo_diff": snap.value("elo", home) - snap.value("elo", away),
        "exp_poss": league["tau"] + snap.value("tempo", home) + snap.value("tempo", away),
        "exp_ppp_h": league["mu"] + snap.value("off", home) - snap.value("def", away) + edge,
        "exp_ppp_a": league["mu"] + snap.value("off", away) - snap.value("def", home) - edge,
        "exp_poss_1h": league["tau_1h"]
        + snap.value("tempo_1h", home)
        + snap.value("tempo_1h", away),
        "exp_ppp_1h_h": league["mu_1h"]
        + snap.value("off_1h", home)
        - snap.value("def_1h", away)
        + edge,
        "exp_ppp_1h_a": league["mu_1h"]
        + snap.value("off_1h", away)
        - snap.value("def_1h", home)
        - edge,
        "neutral": 1.0 if game.neutral else 0.0,
        "hca": league["hca"],
        "postseason": 1.0 if game.postseason else 0.0,
    }
    for factor in ("orb", "tov", "efg", "ftr"):
        level = league[f"mu_{factor}"]
        features[f"{factor}_matchup_h"] = (
            level + snap.value(f"{factor}_o", home) - snap.value(f"{factor}_d", away)
        )
        features[f"{factor}_matchup_a"] = (
            level + snap.value(f"{factor}_o", away) - snap.value(f"{factor}_d", home)
        )

    for side, team in (("h", game.home_id), ("a", game.away_id)):
        schedule = _schedule_features(
            engine, game.season, engine.history.pooled_team(game.season, team), game.tip_utc, as_of
        )
        for name, value in schedule.items():
            features[f"{name}_{side}"] = value
        lost, starters = _availability(engine, game.season, team, as_of)
        features[f"avail_missing_{side}"] = lost
        features[f"avail_starters_{side}"] = starters

    data = engine.history.season(game.season)
    first_tip = int(data.tips[0]) if len(data.tips) else None
    started = first_tip is not None and first_tip < to_micros(as_of)
    features["days_since_start"] = (
        (to_micros(as_of) - first_tip) / DAY_MICROS if started and first_tip is not None else 0.0
    )
    features["month"] = float(game.slate_date_et.month)
    features["is_weekend"] = 1.0 if game.slate_date_et.weekday() >= SATURDAY else 0.0

    ordered = {name: features[name] for name in FEATURES}
    return FeatureRow(
        game_id=game.game_id,
        as_of=as_of,
        feature_set_version=FEATURE_SET_VERSION,
        feature_hash=hash_features(ordered),
        features=ordered,
    )
