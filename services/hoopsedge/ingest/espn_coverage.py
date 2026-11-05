"""Checks on raw ESPN responses, used by scripts/espn_probe.py (docs/01 section 0.3).

These measure what ESPN provides. The curated parsers come in Phase 2.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REGULATION_PERIODS = {"NBA": 4, "NCAA": 2}
REGULATION_MINUTES = {"NBA": 48, "NCAA": 40}
OVERTIME_MINUTES = 5
POSSESSION_RANGE = {"NBA": (80.0, 120.0), "NCAA": (50.0, 90.0)}

# These appear as odds "providers" but publish projections, aggregates or labels, not book prices.
NON_BOOK_PROVIDERS = frozenset(
    {"numberfire", "teamrankings", "consensus", "accuscore", "opening", "unknown", "fantasy911.com"}
)
IN_PLAY_MARKER = "live odds"
EXHIBITION_COMPETITION_TYPE = "ALLSTAR"
# ESPN files some of these as standard games, so the competition type alone misses them.
EXHIBITION_HEADLINES = ("all-star", "rising stars")


@dataclass(frozen=True)
class GameCheck:
    game_id: str
    season_type: int | None
    et_date: date
    is_final: bool
    is_exhibition: bool
    periods_present: bool
    periods_match_score: bool
    half_available: bool
    ot_periods: int
    neutral_known: bool
    venue_known: bool


@dataclass(frozen=True)
class SummaryCheck:
    missing_fields: tuple[str, ...]
    player_minutes_present: bool
    starter_flag_present: bool
    possessions_plausible: bool | None
    turnover_relations: tuple[str, ...]
    injury_rows: int
    injury_rows_after_tip: int
    pickcenter_providers: tuple[str, ...]


@dataclass(frozen=True)
class OddsCheck:
    all_providers: tuple[str, ...]
    book_providers: tuple[str, ...]
    has_book_line: bool
    has_open_close: bool
    spread_sign: str | None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def et_date(value: str) -> date:
    return parse_time(value).astimezone(EASTERN).date()


def plausible_possessions(possessions: float, league: str, ot_periods: int) -> bool:
    low, high = POSSESSION_RANGE[league]
    scale = 1 + ot_periods * OVERTIME_MINUTES / REGULATION_MINUTES[league]
    return low * scale <= possessions <= high * scale


def check_scoreboard_event(event: dict[str, Any], league: str) -> GameCheck:
    competition = event["competitions"][0]
    competitors = competition.get("competitors", [])
    periods = [[line.get("value") for line in c.get("linescores") or []] for c in competitors]
    regulation = REGULATION_PERIODS[league]
    half_periods = regulation // 2

    complete = len(periods) == 2 and all(
        len(side) >= regulation and all(value is not None for value in side) for side in periods
    )
    return GameCheck(
        game_id=str(event["id"]),
        season_type=event.get("season", {}).get("type"),
        et_date=et_date(event["date"]),
        is_final=competition.get("status", {}).get("type", {}).get("name") == "STATUS_FINAL",
        is_exhibition=_is_exhibition(competition),
        periods_present=complete,
        periods_match_score=complete
        and all(
            to_number(c.get("score")) == sum(side)
            for c, side in zip(competitors, periods, strict=True)
        ),
        half_available=complete and all(len(side) >= half_periods for side in periods),
        ot_periods=max(len(periods[0]) - regulation, 0) if complete else 0,
        neutral_known=isinstance(competition.get("neutralSite"), bool),
        venue_known=bool(competition.get("venue", {}).get("id")),
    )


def check_summary(summary: dict[str, Any], league: str) -> SummaryCheck:
    header = summary.get("header", {}).get("competitions", [{}])[0]
    box = summary.get("boxscore", {})
    team_stats = [
        {stat["name"]: stat.get("displayValue") for stat in team.get("statistics", [])}
        for team in box.get("teams", [])
    ]
    player_groups = box.get("players", [])
    tip = header.get("date")
    injury_rows = [
        row for group in summary.get("injuries") or [] for row in group.get("injuries", [])
    ]
    return SummaryCheck(
        missing_fields=_missing_fields(team_stats, header),
        player_minutes_present=_players_have_minutes(player_groups),
        starter_flag_present=_players_have_starter_flag(player_groups),
        possessions_plausible=_possessions_plausible(team_stats, player_groups, header, league),
        turnover_relations=tuple(
            _turnover_relation(stats, group)
            for stats, group in zip(team_stats, player_groups, strict=False)
        ),
        injury_rows=len(injury_rows),
        injury_rows_after_tip=sum(
            1
            for row in injury_rows
            if tip and row.get("date") and parse_time(row["date"]) > parse_time(tip)
        ),
        pickcenter_providers=tuple(
            str(item.get("provider", {}).get("name")) for item in summary.get("pickcenter") or []
        ),
    )


def check_core_odds(document: dict[str, Any]) -> OddsCheck:
    items = document.get("items", [])
    names = [str(item.get("provider", {}).get("name")) for item in items]
    books = [item for item in items if is_book_line(item)]
    return OddsCheck(
        all_providers=tuple(names),
        book_providers=tuple(str(item["provider"]["name"]) for item in books),
        has_book_line=bool(books),
        has_open_close=any(item.get("open") and item.get("close") for item in books),
        spread_sign=_spread_sign(books[0]) if books else None,
    )


def _is_exhibition(competition: dict[str, Any]) -> bool:
    if competition.get("type", {}).get("abbreviation") == EXHIBITION_COMPETITION_TYPE:
        return True
    headlines = [str(note.get("headline", "")).lower() for note in competition.get("notes") or []]
    return any(marker in text for text in headlines for marker in EXHIBITION_HEADLINES)


def is_book_line(item: dict[str, Any]) -> bool:
    name = str(item.get("provider", {}).get("name")).lower()
    return (
        name not in NON_BOOK_PROVIDERS
        and IN_PLAY_MARKER not in name
        and item.get("spread") is not None
        and item.get("overUnder") is not None
    )


def _missing_fields(team_stats: list[dict[str, Any]], header: dict[str, Any]) -> tuple[str, ...]:
    required = {
        "FG": "fieldGoalsMade-fieldGoalsAttempted",
        "3P": "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "FT": "freeThrowsMade-freeThrowsAttempted",
        "OREB": "offensiveRebounds",
        "DREB": "defensiveRebounds",
        "TOV": "totalTurnovers",
        "PF": "fouls",
    }
    missing = [
        label
        for label, name in required.items()
        if len(team_stats) != 2 or any(not stats.get(name) for stats in team_stats)
    ]
    competitors = header.get("competitors", [])
    if len(competitors) != 2 or any(to_number(c.get("score")) is None for c in competitors):
        missing.append("PTS")
    return tuple(missing)


def _players_have_minutes(groups: list[dict[str, Any]]) -> bool:
    if len(groups) != 2:
        return False
    for group in groups:
        block = (group.get("statistics") or [{}])[0]
        keys = block.get("keys") or []
        if "minutes" not in keys:
            return False
        index = keys.index("minutes")
        played = [a for a in block.get("athletes", []) if a.get("stats")]
        if not played or not any(to_number(a["stats"][index]) for a in played):
            return False
    return True


def _players_have_starter_flag(groups: list[dict[str, Any]]) -> bool:
    if len(groups) != 2:
        return False
    return all(
        any(a.get("starter") is True for a in (g.get("statistics") or [{}])[0].get("athletes", []))
        for g in groups
    )


def _possessions_plausible(
    team_stats: list[dict[str, Any]],
    player_groups: list[dict[str, Any]],
    header: dict[str, Any],
    league: str,
) -> bool | None:
    if len(team_stats) != 2 or len(player_groups) != 2:
        return None
    estimates = [
        _possessions(stats, group) for stats, group in zip(team_stats, player_groups, strict=True)
    ]
    if any(value is None for value in estimates):
        return None
    average = sum(value for value in estimates if value is not None) / 2
    competitors = header.get("competitors", [])
    played = len(competitors[0].get("linescores") or []) if competitors else 0
    ot_periods = max(played - REGULATION_PERIODS[league], 0)
    return plausible_possessions(average, league, ot_periods)


def _possessions(stats: dict[str, Any], group: dict[str, Any]) -> float | None:
    """FGA - OREB + TOV + 0.44 * FTA, per docs/03-data-ingestion.md section 2.3.

    TOV is player turnovers plus team turnovers, because the reported team total is wrong
    in some 2017-18 NBA games.
    """
    fga = attempts(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
    fta = attempts(stats.get("freeThrowsMade-freeThrowsAttempted"))
    oreb = to_number(stats.get("offensiveRebounds"))
    players = player_turnovers(group)
    team = to_number(stats.get("teamTurnovers"))
    if fga is None or fta is None or oreb is None or players is None or team is None:
        return None
    return fga - oreb + players + team + 0.44 * fta


def _turnover_relation(stats: dict[str, Any], group: dict[str, Any]) -> str:
    team = to_number(stats.get("teamTurnovers"))
    total = to_number(stats.get("totalTurnovers"))
    players = player_turnovers(group)
    if team is None or total is None or players is None:
        return "unknown"
    if team == 0 and total == players:
        return "no_team_turnovers"
    if total == players + team:
        return "total_is_players_plus_team"
    if total == players:
        return "total_excludes_team"
    return "other"


def player_turnovers(group: dict[str, Any]) -> float | None:
    block = (group.get("statistics") or [{}])[0]
    keys = block.get("keys") or []
    if "turnovers" not in keys:
        return None
    index = keys.index("turnovers")
    return sum(
        to_number(a["stats"][index]) or 0 for a in block.get("athletes", []) if a.get("stats")
    )


def _spread_sign(item: dict[str, Any]) -> str | None:
    spread = item.get("spread")
    home_favorite = (item.get("homeTeamOdds") or {}).get("favorite")
    if not spread or not isinstance(home_favorite, bool):
        return None
    if (spread < 0) == home_favorite:
        return "negative_means_home_favored"
    return "positive_means_home_favored"


def attempts(made_attempted: Any) -> float | None:
    if not isinstance(made_attempted, str) or "-" not in made_attempted:
        return None
    return to_number(made_attempted.split("-")[1])


def to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
