"""ESPN parsing and validation (docs/03 section 2.3), checked by hand against real responses."""

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from hoopsedge.ingest.espn_parser import parse_game

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def real_game(stem: str, game_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    board = load(f"{stem}-scoreboard.json")
    event = next(e for e in board["events"] if str(e["id"]) == game_id)
    return event, load(f"{stem}-summary.json")


def ncaa_game() -> tuple[dict[str, Any], dict[str, Any]]:
    return real_game("ncaa-20250118", "401708333")


def nba_game() -> tuple[dict[str, Any], dict[str, Any]]:
    return real_game("nba-20250115", "401705127")


NBA_IDS = {"18", "20"}


def test_game_row_from_a_real_ncaa_game() -> None:
    event, summary = ncaa_game()
    game = parse_game("NCAA", event, summary).game
    assert game is not None
    assert game.game_id == "401708333"
    assert (game.league, game.season) == ("NCAA", 2025)
    assert game.tip_utc == datetime(2025, 1, 18, 18, 0, tzinfo=UTC)
    assert game.slate_date_et == date(2025, 1, 18)
    assert (game.home_id, game.away_id) == ("61", "2")
    assert (game.home_pts, game.away_pts) == (68, 70)
    assert game.home_periods == [27, 41]
    assert game.home_1h == 27
    assert game.ot_periods == 0
    assert game.neutral is False
    assert game.venue_id == "2088"
    assert game.postseason is False
    assert game.season_flag is None
    assert game.box_missing is False


def test_team_box_uses_player_plus_team_turnovers_d24() -> None:
    event, summary = ncaa_game()
    result = parse_game("NCAA", event, summary)
    away = next(row for row in result.team_games if row.team_id == "2")
    assert (away.opp_id, away.is_home) == ("61", False)
    assert (away.fgm, away.fga, away.fg3m, away.fg3a, away.ftm, away.fta) == (
        24,
        61,
        10,
        23,
        12,
        20,
    )
    assert (away.oreb, away.dreb, away.pf, away.pts, away.minutes) == (15, 24, 16, 70, 40)
    assert away.tov == 11  # 9 by players plus 2 team, which matches ESPN's total here.


def test_possessions_are_averaged_across_both_teams() -> None:
    event, summary = ncaa_game()
    rows = parse_game("NCAA", event, summary).team_games
    assert len(rows) == 2
    assert rows[0].poss == rows[1].poss
    away = next(row for row in rows if row.team_id == "2")
    home = next(row for row in rows if row.team_id == "61")
    away_estimate = 61 - 15 + 11 + 0.44 * 20  # 65.8
    home_estimate = home.fga - home.oreb + home.tov + 0.44 * home.fta
    assert away.poss == pytest.approx((away_estimate + home_estimate) / 2)


def test_nba_first_half_and_player_rows() -> None:
    event, summary = nba_game()
    result = parse_game("NBA", event, summary, NBA_IDS)
    assert result.game is not None
    assert result.game.home_1h == sum(result.game.home_periods[:2])
    away = next(row for row in result.team_games if row.team_id == "18")
    assert result.game.ot_periods == 1  # This game finished "Final/OT".
    assert (away.tov, away.pts, away.minutes) == (16, 125, 53)
    assert away.pts_1h == sum(result.game.away_periods[:2])
    assert away.fga - away.oreb + away.tov + 0.44 * away.fta == pytest.approx(97.56)

    anunoby = next(row for row in result.player_games if row.player_id == "3934719")
    assert (anunoby.team_id, anunoby.minutes, anunoby.pts, anunoby.starter) == ("18", 45, 17, True)
    knicks = summary["boxscore"]["players"][0]["statistics"][0]["athletes"]
    played = [athlete for athlete in knicks if athlete["stats"]]
    assert len(played) < len(knicks)  # Players who didn't play get no row.
    assert sum(row.team_id == "18" for row in result.player_games) == len(played)
    assert sum(row.pts for row in result.player_games if row.team_id == "18") == 125


def test_ncaa_gets_no_player_rows() -> None:
    event, summary = ncaa_game()
    assert parse_game("NCAA", event, summary).player_games == []


def test_overtime_scales_minutes_and_halves_stay_first_half() -> None:
    event, summary = ncaa_game()
    event = copy.deepcopy(event)
    competitors = event["competitions"][0]["competitors"]
    competitors[0]["linescores"].append({"value": 10.0})
    competitors[1]["linescores"].append({"value": 8.0})
    competitors[0]["score"] = str(int(competitors[0]["score"]) + 10)
    competitors[1]["score"] = str(int(competitors[1]["score"]) + 8)
    result = parse_game("NCAA", event, None)
    assert result.game is not None
    assert result.game.ot_periods == 1
    assert result.game.home_1h == 27
    assert result.game.home_pts == 78


def test_period_sum_mismatch_rejects_the_whole_game() -> None:
    event, summary = ncaa_game()
    event = copy.deepcopy(event)
    event["competitions"][0]["competitors"][0]["score"] = "99"
    result = parse_game("NCAA", event, summary)
    assert result.game is None
    assert result.team_games == []
    assert [(r.table, r.reason) for r in result.rejects] == [
        ("games", "periods_do_not_sum_to_score")
    ]


def test_same_team_twice_rejects_the_game() -> None:
    event, summary = ncaa_game()
    event = copy.deepcopy(event)
    competitors = event["competitions"][0]["competitors"]
    competitors[1]["id"] = competitors[0]["id"]
    result = parse_game("NCAA", event, summary)
    assert result.game is None
    assert result.rejects[0].reason == "team_appears_twice"


def test_missing_home_or_away_rejects_the_game() -> None:
    event, summary = ncaa_game()
    event = copy.deepcopy(event)
    event["competitions"][0]["competitors"][0]["homeAway"] = "away"
    result = parse_game("NCAA", event, summary)
    assert result.game is None
    assert result.rejects[0].reason == "no_home_and_away"


def test_unknown_nba_team_is_rejected_d23() -> None:
    event, summary = nba_game()
    result = parse_game("NBA", event, summary, {"18"})
    assert result.game is None
    assert result.rejects[0].reason == "team_not_in_registry"


def test_empty_box_keeps_the_score_and_flags_it_d25() -> None:
    event, summary = nba_game()
    summary = copy.deepcopy(summary)
    summary["boxscore"] = {"teams": [], "players": []}
    result = parse_game("NBA", event, summary, NBA_IDS)
    assert result.game is not None
    assert result.game.box_missing is True
    assert (result.game.home_pts, result.game.away_pts) == (119, 125)
    assert result.team_games == []
    assert result.player_games == []
    assert result.rejects == []


def test_a_player_with_no_id_is_left_out_and_the_box_is_kept() -> None:
    event, summary = nba_game()
    summary = copy.deepcopy(summary)
    athletes = summary["boxscore"]["players"][0]["statistics"][0]["athletes"]
    keys = summary["boxscore"]["players"][0]["statistics"][0]["keys"]
    athletes.append(
        {
            "athlete": {"links": [], "shortName": "Olbrich"},
            "starter": False,
            "stats": ["--", *["0"] * (len(keys) - 1)],
        }
    )
    result = parse_game("NBA", event, summary, NBA_IDS)
    assert result.rejects == []
    assert result.game is not None
    assert result.game.box_missing is False
    assert len(result.team_games) == 2
    assert all(row.player_id for row in result.player_games)


def test_an_unexpected_box_shape_costs_the_box_and_keeps_the_reason() -> None:
    event, summary = nba_game()
    summary = copy.deepcopy(summary)
    del summary["boxscore"]["players"][0]["team"]
    result = parse_game("NBA", event, summary, NBA_IDS)
    assert result.game is not None
    assert result.game.box_missing is True
    assert result.rejects[0].reason == "box_unreadable"
    assert "KeyError" in (result.rejects[0].detail or "")


def test_no_summary_is_the_same_as_an_empty_box() -> None:
    event, _ = nba_game()
    result = parse_game("NBA", event, None, NBA_IDS)
    assert result.game is not None
    assert result.game.box_missing is True


def test_implausible_possessions_quarantine_the_box_only() -> None:
    event, summary = ncaa_game()
    summary = copy.deepcopy(summary)
    for team in summary["boxscore"]["teams"]:
        for stat in team["statistics"]:
            if stat["name"] == "fieldGoalsMade-fieldGoalsAttempted":
                made = stat["displayValue"].split("-")[0]
                stat["displayValue"] = f"{made}-400"
    result = parse_game("NCAA", event, summary)
    assert result.game is not None
    assert result.game.box_missing is True
    assert result.team_games == []
    assert [r.reason for r in result.rejects] == ["possessions_implausible"]
    assert result.rejects[0].table == "team_games"


def test_box_that_disagrees_with_the_score_is_quarantined() -> None:
    event, summary = ncaa_game()
    summary = copy.deepcopy(summary)
    team = summary["boxscore"]["teams"][0]
    for stat in team["statistics"]:
        if stat["name"] == "freeThrowsMade-freeThrowsAttempted":
            stat["displayValue"] = "1-20"
    result = parse_game("NCAA", event, summary)
    assert result.game is not None
    assert result.game.box_missing is True
    assert [r.reason for r in result.rejects] == ["box_points_do_not_match_score"]


def test_garbled_box_is_quarantined_not_guessed() -> None:
    event, summary = ncaa_game()
    summary = copy.deepcopy(summary)
    for stat in summary["boxscore"]["teams"][0]["statistics"]:
        if stat["name"] == "offensiveRebounds":
            stat["displayValue"] = "--"
    result = parse_game("NCAA", event, summary)
    assert result.game is not None
    assert result.game.box_missing is True
    assert [r.reason for r in result.rejects] == ["box_unreadable"]


@pytest.mark.parametrize(("season_type", "postseason"), [(2, False), (3, True), (5, True)])
def test_postseason_includes_play_in(season_type: int, postseason: bool) -> None:
    event, summary = nba_game()
    event = copy.deepcopy(event)
    event["season"]["type"] = season_type
    game = parse_game("NBA", event, summary, NBA_IDS).game
    assert game is not None
    assert game.postseason is postseason


@pytest.mark.parametrize(
    ("league", "year", "flag"),
    [("NBA", 2020, "covid"), ("NBA", 2021, "covid"), ("NCAA", 2020, "covid"), ("NCAA", 2022, None)],
)
def test_covid_seasons_are_flagged_not_dropped(league: str, year: int, flag: str | None) -> None:
    event, summary = nba_game() if league == "NBA" else ncaa_game()
    event = copy.deepcopy(event)
    event["season"]["year"] = year
    game = parse_game(league, event, summary, NBA_IDS if league == "NBA" else None).game
    assert game is not None
    assert game.season_flag == flag


@pytest.mark.parametrize(
    "stem", ["nba-20250115", "nba-20261104", "ncaa-20170114", "ncaa-20250118", "ncaa-20261104"]
)
def test_every_recorded_game_parses_cleanly(stem: str) -> None:
    board = load(f"{stem}-scoreboard.json")
    summary = load(f"{stem}-summary.json")
    game_id = str(summary["header"]["id"])
    event = next(e for e in board["events"] if str(e["id"]) == game_id)
    league = "NBA" if stem.startswith("nba") else "NCAA"
    result = parse_game(league, event, summary)
    assert result.rejects == []
    assert result.game is not None
    assert result.game.box_missing is False
    assert len(result.team_games) == 2
