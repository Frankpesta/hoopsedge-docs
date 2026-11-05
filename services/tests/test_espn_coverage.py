"""Tests for the ESPN coverage checks, on inputs shaped like recorded responses."""

from datetime import date
from typing import Any

import pytest

from hoopsedge.ingest.espn_coverage import (
    check_core_odds,
    check_scoreboard_event,
    check_summary,
    et_date,
    plausible_possessions,
)


def event(
    home: list[int] | None = None,
    away: list[int] | None = None,
    home_score: str = "109",
    away_score: str = "95",
    status: str = "STATUS_FINAL",
    tip: str = "2025-01-16T00:00Z",
    season_type: int = 2,
    competition_type: str = "STD",
    headline: str | None = None,
) -> dict[str, Any]:
    home = [25, 25, 30, 29] if home is None else home
    away = [20, 25, 25, 25] if away is None else away

    def competitor(side: str, periods: list[int], score: str) -> dict[str, Any]:
        return {
            "homeAway": side,
            "score": score,
            "linescores": [{"value": float(v), "period": i + 1} for i, v in enumerate(periods)],
        }

    return {
        "id": "1",
        "date": tip,
        "season": {"year": 2025, "type": season_type},
        "competitions": [
            {
                "neutralSite": False,
                "type": {"abbreviation": competition_type},
                "notes": [{"headline": headline}] if headline else [],
                "venue": {"id": "10"},
                "status": {"type": {"name": status}},
                "competitors": [
                    competitor("home", home, home_score),
                    competitor("away", away, away_score),
                ],
            }
        ],
    }


def team_box(
    field_goals: str = "40-88",
    turnovers: str = "12",
    team_turnovers: str = "0",
    total_turnovers: str = "12",
    omit: tuple[str, ...] = (),
) -> dict[str, Any]:
    stats = {
        "fieldGoalsMade-fieldGoalsAttempted": field_goals,
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "12-35",
        "freeThrowsMade-freeThrowsAttempted": "17-22",
        "offensiveRebounds": "10",
        "defensiveRebounds": "30",
        "turnovers": turnovers,
        "teamTurnovers": team_turnovers,
        "totalTurnovers": total_turnovers,
        "fouls": "18",
    }
    return {
        "statistics": [
            {"name": name, "displayValue": value}
            for name, value in stats.items()
            if name not in omit
        ]
    }


def players(turnovers: list[str], minutes: str = "30", starter: bool = True) -> dict[str, Any]:
    return {
        "statistics": [
            {
                "keys": ["minutes", "points", "turnovers"],
                "athletes": [
                    {"starter": starter, "didNotPlay": False, "stats": [minutes, "10", value]}
                    for value in turnovers
                ]
                + [{"starter": False, "didNotPlay": True, "stats": []}],
            }
        ]
    }


def summary(
    teams: list[dict[str, Any]] | None = None,
    player_rows: list[dict[str, Any]] | None = None,
    scores: tuple[str | None, str | None] = ("109", "95"),
    injury_dates: list[str] | None = None,
    pickcenter: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "header": {
            "competitions": [
                {
                    "date": "2025-01-16T00:00Z",
                    "competitors": [
                        {"homeAway": "home", "score": scores[0], "linescores": [{}, {}, {}, {}]},
                        {"homeAway": "away", "score": scores[1], "linescores": [{}, {}, {}, {}]},
                    ],
                }
            ]
        },
        "boxscore": {
            "teams": [team_box(), team_box()] if teams is None else teams,
            "players": (
                [players(["6", "6"]), players(["6", "6"])] if player_rows is None else player_rows
            ),
        },
        "pickcenter": pickcenter or [],
    }
    if injury_dates is not None:
        result["injuries"] = [{"injuries": [{"date": d, "status": "Out"} for d in injury_dates]}]
    return result


def test_et_date_uses_us_eastern_not_utc() -> None:
    assert et_date("2025-01-16T00:00Z") == date(2025, 1, 15)
    assert et_date("2025-01-16T05:00Z") == date(2025, 1, 16)
    assert et_date("2025-07-16T03:59Z") == date(2025, 7, 15)


@pytest.mark.parametrize(
    ("competition_type", "headline"),
    [
        ("ALLSTAR", "NBA ALL-STAR GAME"),
        ("ALLSTAR", None),
        ("STD", "RISING STARS"),
        ("STD", "NBA All-Star - Round Robin"),
    ],
)
def test_all_star_events_are_exhibitions(competition_type: str, headline: str | None) -> None:
    game = event(competition_type=competition_type, headline=headline)
    assert check_scoreboard_event(game, "NBA").is_exhibition


@pytest.mark.parametrize(
    ("competition_type", "headline"),
    [
        ("STD", None),
        ("STD", "NBA Cup - Group Play"),
        ("CC", "NBA Cup Championship"),
        ("STD", "NBA Paris Games 2025"),
        ("STD", "Rescheduled from Jan 11"),
    ],
)
def test_real_games_are_not_exhibitions(competition_type: str, headline: str | None) -> None:
    game = event(competition_type=competition_type, headline=headline)
    assert not check_scoreboard_event(game, "NBA").is_exhibition


def test_final_nba_game_with_complete_periods() -> None:
    check = check_scoreboard_event(event(), "NBA")
    assert check.is_final
    assert check.periods_present
    assert check.periods_match_score
    assert check.half_available
    assert check.ot_periods == 0
    assert check.et_date == date(2025, 1, 15)
    assert check.neutral_known
    assert check.venue_known


def test_counts_overtime_periods() -> None:
    check = check_scoreboard_event(
        event(
            home=[21, 26, 38, 24, 10], away=[30, 30, 25, 24, 16], home_score="119", away_score="125"
        ),
        "NBA",
    )
    assert check.ot_periods == 1
    assert check.periods_match_score


def test_ncaa_has_two_regulation_periods() -> None:
    check = check_scoreboard_event(
        event(home=[27, 41], away=[33, 37], home_score="68", away_score="70"), "NCAA"
    )
    assert check.half_available
    assert check.ot_periods == 0
    assert check.periods_match_score


def test_period_sum_mismatch_is_detected() -> None:
    assert not check_scoreboard_event(event(home_score="110"), "NBA").periods_match_score


def test_missing_periods_means_no_half_score() -> None:
    check = check_scoreboard_event(event(home=[], away=[]), "NBA")
    assert not check.periods_present
    assert not check.half_available
    assert not check.periods_match_score


def test_nba_with_one_period_has_no_half_score() -> None:
    assert not check_scoreboard_event(event(home=[25], away=[20]), "NBA").half_available


def test_non_final_game_is_flagged() -> None:
    assert not check_scoreboard_event(event(status="STATUS_POSTPONED"), "NBA").is_final


def test_season_type_is_recorded() -> None:
    assert check_scoreboard_event(event(season_type=3), "NBA").season_type == 3


def test_complete_box_score() -> None:
    check = check_summary(summary(), "NBA")
    assert check.missing_fields == ()
    assert check.player_minutes_present
    assert check.starter_flag_present
    assert check.possessions_plausible


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("fieldGoalsMade-fieldGoalsAttempted", "FG"),
        ("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "3P"),
        ("freeThrowsMade-freeThrowsAttempted", "FT"),
        ("offensiveRebounds", "OREB"),
        ("defensiveRebounds", "DREB"),
        ("totalTurnovers", "TOV"),
        ("fouls", "PF"),
    ],
)
def test_missing_box_field_is_reported(field: str, label: str) -> None:
    check = check_summary(summary(teams=[team_box(omit=(field,)), team_box()]), "NBA")
    assert label in check.missing_fields
    assert check.possessions_plausible is (None if label in {"FG", "FT", "OREB"} else True)


def test_missing_score_is_reported() -> None:
    assert "PTS" in check_summary(summary(scores=(None, "95")), "NBA").missing_fields


def test_empty_box_score_reports_everything_missing() -> None:
    check = check_summary(summary(teams=[], player_rows=[]), "NCAA")
    assert set(check.missing_fields) >= {"FG", "3P", "FT", "OREB", "DREB", "TOV", "PF"}
    assert not check.player_minutes_present
    assert check.possessions_plausible is None


def test_detects_team_turnovers_added_to_total() -> None:
    box = team_box(turnovers="13", team_turnovers="3", total_turnovers="16")
    check = check_summary(
        summary(teams=[box, team_box()], player_rows=[players(["7", "6"]), players(["6", "6"])]),
        "NBA",
    )
    assert check.turnover_relations == ("total_is_players_plus_team", "no_team_turnovers")


def test_detects_total_that_already_includes_team_turnovers() -> None:
    box = team_box(turnovers="11", team_turnovers="2", total_turnovers="11")
    check = check_summary(
        summary(teams=[box, team_box()], player_rows=[players(["5", "4"]), players(["6", "6"])]),
        "NCAA",
    )
    assert check.turnover_relations == ("total_is_players_plus_team", "no_team_turnovers")


def test_detects_total_that_excludes_team_turnovers() -> None:
    box = team_box(turnovers="11", team_turnovers="2", total_turnovers="11")
    check = check_summary(
        summary(teams=[box, team_box()], player_rows=[players(["6", "5"]), players(["6", "6"])]),
        "NCAA",
    )
    assert check.turnover_relations == ("total_excludes_team", "no_team_turnovers")


def test_possessions_use_player_turnovers_when_the_team_total_is_wrong() -> None:
    """2017-18 games report totals of 0-2 while player turnovers sum to 9-22."""
    broken = team_box(field_goals="36-75", turnovers="0", team_turnovers="0", total_turnovers="0")
    check = check_summary(
        summary(teams=[broken, broken], player_rows=[players(["7", "7"]), players(["7", "7"])]),
        "NBA",
    )
    assert check.possessions_plausible is True
    assert check.turnover_relations == ("other", "other")


def test_possessions_add_team_turnovers_to_player_turnovers() -> None:
    box = team_box(field_goals="36-75", turnovers="10", team_turnovers="4", total_turnovers="14")
    low = check_summary(
        summary(teams=[box, box], player_rows=[players(["5", "5"]), players(["5", "5"])]), "NBA"
    )
    assert low.possessions_plausible is True


def test_possessions_unknown_without_team_turnovers() -> None:
    box = team_box(omit=("teamTurnovers",))
    assert check_summary(summary(teams=[box, box]), "NBA").possessions_plausible is None


def test_counts_injury_rows_dated_after_the_game() -> None:
    check = check_summary(
        summary(injury_dates=["2025-01-15T12:00Z", "2026-11-05T02:23Z", "2026-11-05T03:00Z"]),
        "NBA",
    )
    assert check.injury_rows == 3
    assert check.injury_rows_after_tip == 2


def test_no_injury_block_counts_zero() -> None:
    check = check_summary(summary(), "NCAA")
    assert check.injury_rows == 0
    assert check.injury_rows_after_tip == 0


def test_pickcenter_provider_names_are_collected() -> None:
    check = check_summary(
        summary(
            pickcenter=[{"provider": {"name": "numberfire"}}, {"provider": {"name": "DraftKings"}}]
        ),
        "NBA",
    )
    assert check.pickcenter_providers == ("numberfire", "DraftKings")


@pytest.mark.parametrize(
    ("league", "poss", "ot", "expected"),
    [
        ("NBA", 100.0, 0, True),
        ("NBA", 79.0, 0, False),
        ("NBA", 121.0, 0, False),
        ("NBA", 125.0, 1, True),
        ("NCAA", 70.0, 0, True),
        ("NCAA", 49.0, 0, False),
        ("NCAA", 91.0, 0, False),
        ("NCAA", 95.0, 1, True),
    ],
)
def test_possession_ranges_scale_for_overtime(
    league: str, poss: float, ot: int, expected: bool
) -> None:
    assert plausible_possessions(poss, league, ot) is expected


def odds_item(
    name: str,
    spread: float | None = -5.5,
    total: float | None = 220.5,
    with_close: bool = False,
    home_favorite: bool | None = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "provider": {"name": name},
        "spread": spread,
        "overUnder": total,
        "homeTeamOdds": {"favorite": home_favorite},
    }
    if with_close:
        item["open"] = {"total": {"american": "219.5"}}
        item["close"] = {"total": {"american": "220.5"}}
    return item


def test_counts_sportsbook_lines_and_ignores_projection_sources() -> None:
    check = check_core_odds(
        {
            "items": [
                odds_item("BETONLINE.ag"),
                odds_item("numberfire"),
                odds_item("teamrankings"),
                odds_item("consensus"),
            ]
        }
    )
    assert check.book_providers == ("BETONLINE.ag",)
    assert check.has_book_line
    assert check.all_providers == ("BETONLINE.ag", "numberfire", "teamrankings", "consensus")


@pytest.mark.parametrize(
    "name",
    [
        "accuscore",
        "Opening",
        "unknown",
        "Fantasy911.com",
        "ESPN Bet - Live Odds",
        "Caesars (New Jersey) - Live Odds",
    ],
)
def test_non_book_and_in_play_sources_do_not_count_as_lines(name: str) -> None:
    check = check_core_odds({"items": [odds_item(name)]})
    assert not check.has_book_line
    assert check.all_providers == (name,)


def test_book_line_needs_spread_and_total() -> None:
    assert not check_core_odds({"items": [odds_item("BookA", total=None)]}).has_book_line
    assert not check_core_odds({"items": [odds_item("BookA", spread=None)]}).has_book_line


def test_empty_odds_document() -> None:
    check = check_core_odds({"items": []})
    assert not check.has_book_line
    assert not check.has_open_close


def test_open_and_close_are_detected() -> None:
    assert check_core_odds({"items": [odds_item("BookA", with_close=True)]}).has_open_close
    assert not check_core_odds({"items": [odds_item("BookA")]}).has_open_close


@pytest.mark.parametrize(
    ("spread", "home_favorite", "expected"),
    [
        (-5.5, True, "negative_means_home_favored"),
        (5.5, False, "negative_means_home_favored"),
        (8.0, True, "positive_means_home_favored"),
        (-8.0, False, "positive_means_home_favored"),
        (0.0, True, None),
        (-3.0, None, None),
    ],
)
def test_spread_sign_convention(
    spread: float, home_favorite: bool | None, expected: str | None
) -> None:
    check = check_core_odds(
        {"items": [odds_item("BookA", spread=spread, home_favorite=home_favorite)]}
    )
    assert check.spread_sign == expected
