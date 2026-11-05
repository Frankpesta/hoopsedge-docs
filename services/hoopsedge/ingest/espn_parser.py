"""Turn one ESPN game into curated rows, or quarantine it with a reason (docs/03 section 2.3).

The game row comes from the scoreboard event, where Phase 0 measured half scores at 100%.
The box comes from the summary. A bad box costs only the box: the score stays usable.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from hoopsedge.common.schemas import Game, PlayerGame, Reject, TeamGame
from hoopsedge.ingest.espn_coverage import (
    OVERTIME_MINUTES,
    REGULATION_MINUTES,
    REGULATION_PERIODS,
    attempts,
    check_scoreboard_event,
    parse_time,
    plausible_possessions,
    player_turnovers,
    to_number,
)

POSTSEASON_TYPES = frozenset({3, 5})  # 3 is the postseason, 5 is the NBA play-in.
COVID_SEASONS = {"NBA": frozenset({2020, 2021}), "NCAA": frozenset({2020, 2021})}
PLAYER_LEAGUES = frozenset({"NBA"})


class _BoxError(ValueError):
    """The box can't be used. Its message becomes the reject reason."""


@dataclass
class ParseResult:
    game: Game | None = None
    team_games: list[TeamGame] = field(default_factory=list)
    player_games: list[PlayerGame] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)


def _period_values(competitor: dict[str, Any]) -> list[int]:
    return [int(line["value"]) for line in competitor.get("linescores") or []]


def parse_game(
    league: str,
    event: dict[str, Any],
    summary: dict[str, Any] | None,
    known_team_ids: Collection[str] | None = None,
) -> ParseResult:
    """Parse one final game. `known_team_ids` rejects teams outside the registry (D23)."""
    game_id = str(event["id"])

    def game_reject(reason: str) -> ParseResult:
        return ParseResult(
            rejects=[Reject(table="games", league=league, game_id=game_id, reason=reason)]
        )

    competition = event["competitions"][0]
    competitors = competition.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if len(competitors) != 2 or home is None or away is None:
        return game_reject("no_home_and_away")
    home_id, away_id = str(home["id"]), str(away["id"])
    if home_id == away_id:
        return game_reject("team_appears_twice")
    if known_team_ids is not None and not {home_id, away_id} <= set(known_team_ids):
        return game_reject("team_not_in_registry")

    check = check_scoreboard_event(event, league)
    if not check.periods_present:
        return game_reject("periods_missing")
    if not check.periods_match_score:
        return game_reject("periods_do_not_sum_to_score")

    half = REGULATION_PERIODS[league] // 2
    home_periods, away_periods = _period_values(home), _period_values(away)
    home_1h, away_1h = sum(home_periods[:half]), sum(away_periods[:half])
    home_pts, away_pts = int(home["score"]), int(away["score"])
    season = int(event["season"]["year"])

    result = ParseResult()
    box_present = bool(summary and summary.get("boxscore", {}).get("teams"))
    if box_present and summary is not None:
        try:
            result.team_games, result.player_games = _parse_box(
                league,
                game_id,
                summary["boxscore"],
                {
                    home_id: (away_id, True, home_pts, home_1h),
                    away_id: (home_id, False, away_pts, away_1h),
                },
                check.ot_periods,
            )
        except _BoxError as error:
            result.team_games, result.player_games = [], []
            result.rejects.append(
                Reject(table="team_games", league=league, game_id=game_id, reason=str(error))
            )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            # An unexpected shape costs this game's box only, and the reason is kept.
            result.team_games, result.player_games = [], []
            result.rejects.append(
                Reject(
                    table="team_games",
                    league=league,
                    game_id=game_id,
                    reason="box_unreadable",
                    detail=f"{type(error).__name__}: {error}",
                )
            )

    venue = competition.get("venue", {}).get("id")
    result.game = Game(
        league=league,
        season=season,
        game_id=game_id,
        tip_utc=parse_time(event["date"]),
        slate_date_et=check.et_date,
        home_id=home_id,
        away_id=away_id,
        neutral=bool(competition.get("neutralSite")),
        venue_id=str(venue) if venue else None,
        status=str(competition["status"]["type"]["name"]),
        home_pts=home_pts,
        away_pts=away_pts,
        home_1h=home_1h,
        away_1h=away_1h,
        home_periods=home_periods,
        away_periods=away_periods,
        ot_periods=check.ot_periods,
        postseason=event["season"].get("type") in POSTSEASON_TYPES,
        season_flag="covid" if season in COVID_SEASONS[league] else None,
        box_missing=not result.team_games,
    )
    return result


def _whole(value: float | None) -> int:
    if value is None:
        raise _BoxError("box_unreadable")
    return int(value)


def _parse_box(
    league: str,
    game_id: str,
    box: dict[str, Any],
    sides: dict[str, tuple[str, bool, int, int]],
    ot_periods: int,
) -> tuple[list[TeamGame], list[PlayerGame]]:
    """`sides` maps each team to (opponent, is_home, points, first-half points)."""
    teams = {str(team["team"]["id"]): team for team in box["teams"]}
    players = {str(group["team"]["id"]): group for group in box.get("players") or []}
    if set(teams) != set(sides) or len(box["teams"]) != 2 or set(players) != set(sides):
        raise _BoxError("box_unreadable")

    rows: list[dict[str, Any]] = []
    estimates: list[float] = []
    for team_id, (opp_id, is_home, points, first_half) in sides.items():
        stats = {stat["name"]: stat.get("displayValue") for stat in teams[team_id]["statistics"]}
        fg = _made_attempted(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
        fg3 = _made_attempted(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        ft = _made_attempted(stats.get("freeThrowsMade-freeThrowsAttempted"))
        oreb = _whole(to_number(stats.get("offensiveRebounds")))
        dreb = _whole(to_number(stats.get("defensiveRebounds")))
        fouls = _whole(to_number(stats.get("fouls")))
        # D24: player turnovers plus team turnovers, not ESPN's reported total.
        by_players = player_turnovers(players[team_id])
        by_team = to_number(stats.get("teamTurnovers"))
        tov = _whole(None if by_players is None or by_team is None else by_players + by_team)
        if 2 * fg[0] + fg3[0] + ft[0] != points:
            raise _BoxError("box_points_do_not_match_score")
        estimates.append(fg[1] - oreb + tov + 0.44 * ft[1])
        rows.append(
            {
                "game_id": game_id,
                "team_id": team_id,
                "opp_id": opp_id,
                "is_home": is_home,
                "fgm": fg[0],
                "fga": fg[1],
                "fg3m": fg3[0],
                "fg3a": fg3[1],
                "ftm": ft[0],
                "fta": ft[1],
                "oreb": oreb,
                "dreb": dreb,
                "tov": tov,
                "pf": fouls,
                "pts": points,
                "pts_1h": first_half,
                "minutes": REGULATION_MINUTES[league] + ot_periods * OVERTIME_MINUTES,
            }
        )
    possessions = sum(estimates) / 2
    if not plausible_possessions(possessions, league, ot_periods):
        raise _BoxError("possessions_implausible")

    team_games = [TeamGame(**row, poss=possessions) for row in rows]
    player_games = (
        [row for team_id in sides for row in _player_rows(game_id, team_id, players[team_id])]
        if league in PLAYER_LEAGUES
        else []
    )
    return team_games, player_games


def _made_attempted(value: Any) -> tuple[int, int]:
    made = to_number(value.split("-")[0]) if isinstance(value, str) and "-" in value else None
    return _whole(made), _whole(attempts(value))


def _player_rows(game_id: str, team_id: str, group: dict[str, Any]) -> list[PlayerGame]:
    block = (group.get("statistics") or [{}])[0]
    keys = block.get("keys") or []
    if "minutes" not in keys or "points" not in keys:
        raise _BoxError("box_unreadable")
    minutes_at, points_at = keys.index("minutes"), keys.index("points")
    return [
        PlayerGame(
            game_id=game_id,
            team_id=team_id,
            player_id=str(athlete["athlete"]["id"]),
            minutes=int(to_number(athlete["stats"][minutes_at]) or 0),
            pts=int(to_number(athlete["stats"][points_at]) or 0),
            starter=athlete.get("starter") is True,
        )
        for athlete in block.get("athletes", [])
        # A few two-way players have no ESPN ID, nearly always with no minutes. The row can't be
        # attributed to anyone, so it's left out and the team's box stays usable (D53).
        if athlete.get("stats") and athlete.get("athlete", {}).get("id")
    ]
