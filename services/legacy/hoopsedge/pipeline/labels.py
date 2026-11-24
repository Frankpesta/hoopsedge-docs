"""Human words for a leg, so the dashboard shows "Duke -6.5" rather than IDs (docs/10 section 9.3).

`home` is the canonical home team, and a handicap's line is on that team.
"""

from typing import Any

from hoopsedge.engine.schemas import Leg


def _number(value: float) -> str:
    return f"{value:g}"


def matchup(home: str, away: str) -> str:
    return f"{away} at {home}"


def pick_label(leg: Leg, home: str, away: str) -> str:
    direction = leg.side.capitalize()
    if leg.family == "handicap":
        team, line = (home, leg.line) if leg.side == "home" else (away, -leg.line)
        return f"{team} {line:+g}"
    if leg.family == "total_1h":
        return f"1H {direction} {_number(leg.line)}"
    if leg.family == "team_total":
        team = home if leg.team == "home" else away
        return f"{team} {direction} {_number(leg.line)}"
    return f"{direction} {_number(leg.line)}"


def leg_summary(leg: Leg, home: str, away: str) -> dict[str, Any]:
    """Enough to show a leg on the Today page, kept on the ticket so one query can serve it."""
    return {
        "rank": leg.rank,
        "game_id": leg.game_id,
        "matchup": matchup(home, away),
        "pick": pick_label(leg, home, away),
        "odds": leg.odds,
        "p_calibrated": leg.p_calibrated,
        "fair": leg.fair,
        "edge": leg.edge,
        "tip_time": leg.tip_time.isoformat(),
    }
