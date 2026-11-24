"""L5 model units: window and leakage, tiers, lean, team pick, flags, provisional tiers, config."""

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from hoopsedge.common.config_v2 import TierThresholds, load_defaults, parse_config
from hoopsedge.ingest.scores import RESULT_SCHEMA
from hoopsedge.l5.data import completed_results
from hoopsedge.l5.model import (
    CROSS_LEVEL_GAME,
    CROSS_LEVEL_L5,
    PROVISIONAL_TIERS,
    SKIP_MIN_GAMES,
    Result,
    TeamInput,
    attack_tier,
    defence_tier,
    fixed_tiers,
    lean,
    predict_game,
    provisional_tiers,
    team_input,
    team_pick,
)

CONFIG = load_defaults()
NCAAF = CONFIG.sports["NCAAF"]
TIERS = fixed_tiers(NCAAF)
START = datetime(2026, 9, 5, 17, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


def game(
    week: int, home: str, away: str, home_pts: int, away_pts: int, season: int = 2026
) -> Result:
    return Result(
        season, f"g{week}{home}{away}", START + week * WEEK, home, away, home_pts, away_pts
    )


def known(pf: float, pa: float, n: int = 5, team_id: str = "t") -> TeamInput:
    return TeamInput(team_id, pf, pa, n, (), 0)


# Window and leakage


def test_window_is_the_last_n_games_newest_first() -> None:
    results = [game(week, "A", f"o{week}", 10 * week, week) for week in range(1, 8)]
    before = START + 8 * WEEK
    found = team_input("A", results, season=2026, before=before, window=5)
    assert found.n == 5
    assert found.game_ids == tuple(f"g{w}Ao{w}" for w in (7, 6, 5, 4, 3))
    assert found.pf == pytest.approx((70 + 60 + 50 + 40 + 30) / 5)
    assert found.pa == pytest.approx((7 + 6 + 5 + 4 + 3) / 5)


def test_away_games_count_from_the_away_side() -> None:
    found = team_input(
        "A", [game(1, "H", "A", 21, 35)], season=2026, before=START + WEEK * 2, window=5
    )
    assert (found.pf, found.pa) == (35, 21)


def test_a_game_at_or_after_kickoff_is_never_in_the_window() -> None:
    results = [game(1, "A", "B", 30, 20), game(2, "A", "C", 40, 10), game(3, "A", "D", 99, 99)]
    kickoff = START + 2 * WEEK
    found = team_input("A", results, season=2026, before=kickoff, window=5)
    assert found.game_ids == ("g1AB",)  # Week 2 kicks off exactly at `before`: excluded.


def test_other_seasons_never_count() -> None:
    results = [game(1, "A", "B", 30, 20, season=2025), game(2, "A", "C", 40, 10)]
    found = team_input("A", results, season=2026, before=START + 5 * WEEK, window=5)
    assert found.game_ids == ("g2AC",)


def test_a_naive_cutoff_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        team_input("A", [], season=2026, before=datetime(2026, 11, 21), window=5)


@settings(max_examples=150, deadline=None)
@given(
    past=st.lists(
        st.tuples(st.integers(0, 20), st.integers(0, 70), st.integers(0, 70)), max_size=12
    ),
    future=st.lists(
        st.tuples(st.integers(0, 20), st.integers(0, 70), st.integers(0, 70)),
        min_size=1,
        max_size=8,
    ),
)
def test_leakage_games_from_kickoff_on_change_nothing(
    past: list[tuple[int, int, int]], future: list[tuple[int, int, int]]
) -> None:
    """Invariant 1: adding any game at or after kickoff leaves the window as it was."""
    kickoff = START + 30 * WEEK
    before = [
        Result(2026, f"p{i}", kickoff - timedelta(hours=1 + hours), "A", f"o{i}", a, b)
        for i, (hours, a, b) in enumerate(past)
    ]
    after = [
        Result(2026, f"f{i}", kickoff + timedelta(hours=hours), "A", f"o{i}", a, b)
        for i, (hours, a, b) in enumerate(future)
    ]
    alone = team_input("A", before, season=2026, before=kickoff, window=5)
    assert team_input("A", before + after, season=2026, before=kickoff, window=5) == alone


def test_same_level_only_drops_other_level_opponents() -> None:
    levels = {"A": "FBS", "B": "FBS", "C": "FCS", "D": "FBS"}
    results = [game(1, "A", "B", 30, 20), game(2, "A", "C", 70, 0), game(3, "D", "A", 14, 17)]
    before = START + 4 * WEEK
    everything = team_input("A", results, season=2026, before=before, window=5, levels=levels)
    assert everything.n == 3
    assert everything.cross_level_opponents == 1
    same = team_input(
        "A", results, season=2026, before=before, window=5, levels=levels, same_level_only=True
    )
    assert same.game_ids == ("g3DA", "g1AB")
    assert (same.pf, same.pa, same.cross_level_opponents) == (23.5, 17.0, 0)


def test_completed_results_leave_out_scheduled_games() -> None:
    frame = pl.DataFrame(
        [
            {**_row("1", True), "home_pts": 30, "away_pts": 20},
            {**_row("2", False), "home_pts": None, "away_pts": None},
        ],
        schema=RESULT_SCHEMA,
    )
    assert [result.game_id for result in completed_results(frame)] == ["1"]


def test_a_completed_result_without_a_score_stops_the_run() -> None:
    frame = pl.DataFrame(
        [{**_row("1", True), "home_pts": None, "away_pts": 3}], schema=RESULT_SCHEMA
    )
    with pytest.raises(ValueError, match="no score"):
        completed_results(frame)


def _row(game_id: str, completed: bool) -> dict[str, Any]:
    return {
        "sport": "NCAAF",
        "season": 2026,
        "season_type": 2,
        "week": 1,
        "game_id": game_id,
        "kickoff_utc": START,
        "slate_date_et": START.date(),
        "home_id": "A",
        "away_id": "B",
        "neutral": False,
        "status": "STATUS_FINAL" if completed else "STATUS_SCHEDULED",
        "state": "post" if completed else "pre",
        "completed": completed,
    }


# Tiers and lean


@pytest.mark.parametrize(
    ("pf", "tier"), [(23.9, "Low"), (24.0, "Medium"), (32.9, "Medium"), (33.0, "High")]
)
def test_attack_tier_edges(pf: float, tier: str) -> None:
    assert attack_tier(pf, TIERS) == tier


@pytest.mark.parametrize(
    ("pa", "tier"), [(20.0, "High"), (20.1, "Medium"), (27.9, "Medium"), (28.0, "Low")]
)
def test_defence_tier_edges(pa: float, tier: str) -> None:
    assert defence_tier(pa, TIERS) == tier


def test_tier_edges_hold_against_float_noise() -> None:
    noisy = (26.8 + 25.6 + 21.6) / 3 * 3 - 50.0  # 24.0 with float error.
    assert attack_tier(noisy, TIERS) == "Medium"


@pytest.mark.parametrize(
    ("edge", "expected"),
    [(4.0, "OVER"), (3.99, "PASS"), (-4.99, "PASS"), (-5.0, "UNDER"), (0.0, "PASS")],
)
def test_lean_thresholds_are_inclusive(edge: float, expected: str) -> None:
    assert lean(edge, NCAAF) == expected


def test_lean_reads_thresholds_from_config() -> None:
    strict = NCAAF.model_copy(update={"over_threshold": 6.0})
    assert lean(4.5, NCAAF) == "OVER"
    assert lean(4.5, strict) == "PASS"


# Team pick


def test_team_pick_takes_the_larger_edge() -> None:
    pick = team_pick(30.0, 20.0, home_line=26.5, away_line=21.5, threshold=3.0)
    assert pick is not None
    assert (pick.side, pick.direction, pick.edge) == ("home", "Over", 3.5)


def test_team_pick_under() -> None:
    pick = team_pick(30.0, 14.0, home_line=31.5, away_line=21.5, threshold=3.0)
    assert pick is not None
    assert (pick.side, pick.direction, pick.edge) == ("away", "Under", -7.5)


def test_team_pick_below_threshold_is_none_and_zero_threshold_always_picks() -> None:
    assert team_pick(30.0, 20.0, home_line=28.5, away_line=19.5, threshold=3.0) is None
    pick = team_pick(30.0, 20.0, home_line=28.5, away_line=19.5, threshold=0.0)
    assert pick is not None and pick.side == "home"


def test_a_projection_on_its_line_gives_no_pick_for_that_side() -> None:
    pick = team_pick(28.0, 20.0, home_line=28.0, away_line=18.5, threshold=0.0)
    assert pick is not None and pick.side == "away"
    assert team_pick(28.0, 20.0, home_line=28.0, away_line=None, threshold=0.0) is None


def test_tied_sides_give_no_pick() -> None:
    assert team_pick(30.0, 20.0, home_line=26.0, away_line=24.0, threshold=0.0) is None


# Whole game


def predict(home: TeamInput, away: TeamInput, **extra: Any) -> Any:
    arguments: dict[str, Any] = {
        "config": NCAAF,
        "bands": CONFIG.outlook,
        "tiers": TIERS,
        "main_line": 50.5,
        **extra,
    }
    return predict_game(home, away, **arguments)


def test_projections_follow_the_brief() -> None:
    prediction = predict(known(30.0, 20.0, team_id="H"), known(25.0, 22.0, team_id="A"))
    assert prediction.home_proj == pytest.approx((30.0 + 22.0) / 2)
    assert prediction.away_proj == pytest.approx((25.0 + 20.0) / 2)
    assert prediction.model_total == pytest.approx(48.5)
    assert prediction.sum_of_avgs == pytest.approx(55.0)
    assert prediction.edge == pytest.approx(-2.0)
    assert prediction.lean == "PASS"


def test_too_few_games_skips_the_game() -> None:
    prediction = predict(known(60, 10, n=3), known(60, 10), home_team_line=10.5)
    assert not prediction.eligible
    assert prediction.skip_reason == SKIP_MIN_GAMES
    assert prediction.lean is None
    assert prediction.team_pick is None


def test_no_main_line_gives_no_lean() -> None:
    assert predict(known(30, 20), known(30, 20), main_line=None).lean is None


def test_cross_level_flags() -> None:
    levels = {"H": "FBS", "A": "FCS"}
    away = TeamInput("A", 20, 30, 5, (), cross_level_opponents=1)
    prediction = predict(known(30, 20, team_id="H"), away, levels=levels)
    assert prediction.flags == (CROSS_LEVEL_GAME, CROSS_LEVEL_L5)
    same = predict(
        known(30, 20, team_id="H"), known(20, 30, team_id="A"), levels={"H": "FBS", "A": "FBS"}
    )
    assert same.flags == ()


def test_provisional_tiers_are_flagged_and_use_percentiles() -> None:
    teams = [known(float(pf), float(100 - pf), team_id=str(pf)) for pf in range(60, 91)]
    tiers = provisional_tiers(teams + [known(200.0, 0.0, n=2)], min_games=4)
    assert tiers.provisional
    assert tiers.atk == pytest.approx((70.0, 80.0))
    assert tiers.defence == pytest.approx((20.0, 30.0))
    prediction = predict(known(85, 15, team_id="H"), known(65, 35, team_id="A"), tiers=tiers)
    assert prediction.home_tiers == ("High", "High")
    assert prediction.away_tiers == ("Low", "Low")
    assert PROVISIONAL_TIERS in prediction.flags


def test_provisional_tiers_need_enough_teams() -> None:
    with pytest.raises(ValueError, match="no tiers yet"):
        provisional_tiers([known(80, 70), known(70, 60, n=1)], min_games=4)


def test_basketball_sports_have_no_fixed_tiers() -> None:
    with pytest.raises(ValueError, match="provisional"):
        fixed_tiers(CONFIG.sports["NCAAB"])


# Config


def test_defaults_carry_the_owner_decisions() -> None:
    assert CONFIG.tickets.booking_cutoff_minutes == 20  # D86.
    assert CONFIG.tickets.max_legs == 20  # D88, NBA included.
    assert [CONFIG.sports[s].team_total_threshold for s in ("NCAAF", "NCAAB", "NBA")] == [
        3.0,
        4.0,
        4.0,
    ]
    assert not any(sport.l5_same_level_only for sport in CONFIG.sports.values())  # D88.
    assert (NCAAF.over_threshold, NCAAF.under_threshold) == (4.0, 5.0)
    assert CONFIG.dry_run is True
    assert NCAAF.tiers == TierThresholds.model_validate({"atk": (24.0, 33.0), "def": (20.0, 28.0)})


def _document() -> dict[str, Any]:
    return CONFIG.model_dump(by_alias=True)


def test_config_round_trips() -> None:
    assert parse_config(_document()) == CONFIG


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["tickets"].update(booking_cutoff_minutes=10),
        lambda d: d["sports"]["NCAAF"].update(min_games=6),
        lambda d: d["sports"]["NCAAF"].update(over_threshold=-1),
        lambda d: d["sports"]["NCAAF"].update(surprise=True),
        lambda d: d["sports"].pop("NBA"),
        lambda d: d["outlook"].update(low_max=6),
        lambda d: d["sports"]["NCAAF"]["tiers"].update(atk=[33.0, 24.0]),
    ],
    ids=[
        "cutoff-below-20",
        "min-games-over-window",
        "negative",
        "unknown-key",
        "missing-sport",
        "outlook-bands",
        "tiers-reversed",
    ],
)
def test_bad_config_is_refused(change: Any) -> None:
    document = _document()
    change(document)
    with pytest.raises(ValidationError):
        parse_config(document)
