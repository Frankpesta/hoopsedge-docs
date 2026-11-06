"""Invariant 1 and D14: nothing a game couldn't know may reach its features. Fails the build."""

import json
import random
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hoopsedge.features.builder import FeatureRow, features_for_game
from hoopsedge.ratings.engine import Engine
from hoopsedge.ratings.history import RESULT_KNOWN_AFTER, GameRef, History
from tests.synthetic import corrupt_from, empty_injuries, empty_players, season_frames

SERVICES = Path(__file__).resolve().parent.parent
PACKAGE = SERVICES / "hoopsedge"
READERS = re.compile(r"read_table|read_parquet|scan_parquet")
# The only places allowed to read curated tables. Everything else goes through as_of accessors.
READ_ALLOWED = {
    "hoopsedge/ratings/history.py",
    "hoopsedge/ingest/curated.py",
    "hoopsedge/ingest/live.py",  # Compares a game's own delta when checking for corrections.
    "hoopsedge/features/training.py",  # Reads its own output, to append new games only.
    # Point-in-time features, season flags, and market lines that only score predictions.
    "hoopsedge/backtest/data.py",
    "hoopsedge/teams/matcher.py",  # Team names and aliases only. They hold no results.
}
SAMPLE = 500


def make_history(games: pl.DataFrame, team_games: pl.DataFrame) -> History:
    return History("NBA", games, team_games, empty_players(), empty_injuries(), {})


def two_seasons() -> tuple[pl.DataFrame, pl.DataFrame]:
    old_games, old_box = season_frames(2024, days=40, seed=3)
    new_games, new_box = season_frames(2025, days=60, seed=4)
    return pl.concat([old_games, new_games]), pl.concat([old_box, new_box])


def ref_for(row: dict[str, Any]) -> GameRef:
    return GameRef(
        league=row["league"],
        season=row["season"],
        game_id=row["game_id"],
        tip_utc=row["tip_utc"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        neutral=row["neutral"],
        postseason=row["postseason"],
        slate_date_et=row["slate_date_et"],
    )


def canonical(row: FeatureRow) -> str:
    return json.dumps(row.features, sort_keys=True, default=str) + row.feature_hash


def test_mutation_scrambling_the_future_changes_nothing() -> None:
    """Docs/04 section 3.5, test 1: 500 games, byte-identical after corrupting the future."""
    games, box = two_seasons()
    season = games.filter(pl.col("season") == 2025)
    rows = season.to_dicts()
    rng = random.Random(7)  # noqa: S311 - Sampling a test, not security.
    sample = [rng.choice(rows) for _ in range(SAMPLE)]
    clean = Engine(make_history(games, box))
    for index, row in enumerate(sample):
        ref = ref_for(row)
        before = canonical(features_for_game(clean, ref, ref.tip_utc))
        bad_games, bad_box = corrupt_from(games, box, ref.tip_utc, seed=index)
        after = canonical(
            features_for_game(Engine(make_history(bad_games, bad_box)), ref, ref.tip_utc)
        )
        assert before == after, ref.game_id


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed=st.integers(0, 10_000),
    teams=st.integers(4, 14),
    days=st.integers(5, 30),
    cut=st.floats(0.05, 0.95),
    corrupt_seed=st.integers(0, 10_000),
    hours=st.integers(-48, 48),
)
def test_property_future_games_never_change_any_rating_or_feature(
    seed: int, teams: int, days: int, cut: float, corrupt_seed: int, hours: int
) -> None:
    """Docs/04 section 3.5, test 2: random seasons and random as_of values."""
    games, box = season_frames(2025, teams=teams, days=days, seed=seed)
    rows = games.sort("tip_utc").to_dicts()
    row = rows[int(cut * len(rows))]
    ref = ref_for(row)
    as_of = min(ref.tip_utc, ref.tip_utc + timedelta(hours=hours))
    clean = Engine(make_history(games, box))
    bad_games, bad_box = corrupt_from(
        games, box, as_of - RESULT_KNOWN_AFTER, seed=corrupt_seed, delete_from=as_of
    )
    dirty = Engine(make_history(bad_games, bad_box))

    first, second = clean.ratings_as_of(2025, as_of), dirty.ratings_as_of(2025, as_of)
    assert first.games_visible == second.games_visible
    for name in first.values:
        for team in first.team_ids:
            assert first.value(name, team) == second.value(name, team), (name, team)
    assert canonical(features_for_game(clean, ref, as_of)) == canonical(
        features_for_game(dirty, ref, as_of)
    )


def test_a_game_still_being_played_cannot_affect_ratings_d14() -> None:
    """A result counts only once tip + 3 h has passed, however final the data says it is."""
    games, box = season_frames(2025, days=10, seed=5)
    rows = games.sort("tip_utc").to_dicts()
    target = rows[len(rows) // 2]
    just_before = target["tip_utc"] + RESULT_KNOWN_AFTER
    engine = Engine(make_history(games, box))

    at_edge = engine.ratings_as_of(2025, just_before)
    after = engine.ratings_as_of(2025, just_before + timedelta(seconds=1))
    visible_at_edge = sum(r["tip_utc"] + RESULT_KNOWN_AFTER < just_before for r in rows)
    assert at_edge.games_visible == visible_at_edge
    assert after.games_visible > at_edge.games_visible

    # A game that tipped an hour ago has "finished" in the table, but changing it does nothing.
    as_of = target["tip_utc"] + timedelta(hours=1)
    scrambled = games.with_columns(
        pl.when(pl.col("game_id") == target["game_id"])
        .then(pl.lit(200))
        .otherwise(pl.col("home_pts"))
        .alias("home_pts")
    )
    one = Engine(make_history(games, box)).ratings_as_of(2025, as_of)
    two = Engine(make_history(scrambled, box)).ratings_as_of(2025, as_of)
    assert one.value("elo", target["home_id"]) == two.value("elo", target["home_id"])
    assert one.value("off", target["home_id"]) == two.value("off", target["home_id"])


def test_rest_days_ignore_games_at_or_after_this_tip() -> None:
    games, box = season_frames(2025, days=20, seed=6)
    rows = games.sort("tip_utc").to_dicts()
    row = rows[-1]
    ref = ref_for(row)
    engine = Engine(make_history(games, box))
    features = features_for_game(engine, ref, ref.tip_utc).features
    earlier = [
        r["tip_utc"]
        for r in rows
        if r["tip_utc"] < ref.tip_utc and ref.home_id in (r["home_id"], r["away_id"])
    ]
    if earlier:
        days = (ref.tip_utc - max(earlier)).total_seconds() / 86400
        assert features["rest_h"] == pytest.approx(min(days, 7.0))


def test_as_of_after_tip_is_refused() -> None:
    games, box = season_frames(2025, days=5, seed=8)
    ref = ref_for(games.sort("tip_utc").to_dicts()[3])
    engine = Engine(make_history(games, box))
    too_late = ref.tip_utc + timedelta(seconds=1)
    try:
        features_for_game(engine, ref, too_late)
    except ValueError as error:
        assert "tip" in str(error)
    else:
        raise AssertionError("features_for_game accepted an as_of after tip-off")


def test_static_rule_curated_reads_stay_in_the_accessors() -> None:
    """Docs/04 section 3.5, test 3."""
    offenders = [
        path.relative_to(SERVICES).as_posix()
        for path in PACKAGE.rglob("*.py")
        if READERS.search(path.read_text("utf-8"))
        and path.relative_to(SERVICES).as_posix() not in READ_ALLOWED
    ]
    assert offenders == []


def test_static_rule_features_only_reach_data_through_the_engine() -> None:
    for name in ("builder.py", "registry.py"):
        source = (PACKAGE / "features" / name).read_text("utf-8")
        for forbidden in ("SeasonData", "._seasons", "._games", "games_frame", "polars"):
            assert forbidden not in source, (name, forbidden)
