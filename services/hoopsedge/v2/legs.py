"""Candidate legs per ticket kind, and the booking-time re-check (brief §6, §7; audit 17, 19).

The lean is always decided against the main line; the booked line follows the configured mode.
Every leg keeps the main line and its odds as well as the booked line, so grading can compare
line modes (audit item 19). Sides are ESPN's: `team` is "home" or "away" in ESPN's orientation,
and `market_id` is whichever SportyBet market carries that team, after any swap (audit item 13).
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from hoopsedge.common.config_v2 import SportConfig
from hoopsedge.l5.model import GamePrediction, lean
from hoopsedge.v2.lines import (
    AWAY_TOTAL_ID,
    HOME_TOTAL_ID,
    Direction,
    EventLines,
    Line,
    booked_line,
    main_line,
)
from hoopsedge.v2.names import SWAPPED

Kind = Literal["TOTALS_LEAN", "TEAM_TOTALS", "TOTALS_ALL_OVER", "TOTALS_ALL_UNDER"]
KINDS: tuple[Kind, ...] = ("TOTALS_LEAN", "TEAM_TOTALS", "TOTALS_ALL_OVER", "TOTALS_ALL_UNDER")
SAFEST = "safest"


@dataclass(frozen=True)
class Leg:
    kind: Kind
    event_id: str
    game_id: str
    home: str  # ESPN short names, ESPN's orientation.
    away: str
    kickoff: datetime  # SportyBet's start, which is what booking is checked against.
    mode: str
    team: Literal["home", "away"] | None
    direction: Direction
    projection: float  # The model total, or the team's projection.
    market_id: str
    specifier: str
    outcome_id: str
    odds: float
    booked_line: float
    main_line: float
    main_odds: float  # Same direction at the main line.
    edge_main: float  # projection - main line
    edge_booked: float  # projection - booked line
    flags: tuple[str, ...]

    @property
    def cushion(self) -> float:
        """How far the projection clears the booked line in the leg's own direction."""
        return self.edge_booked if self.direction == "Over" else -self.edge_booked

    @property
    def rank(self) -> float:
        return abs(self.edge_main)


def _team_ladder(lines: EventLines, team: str, orientation: str) -> tuple[str, tuple[Line, ...]]:
    sportybet_home = (team == "home") != (orientation == SWAPPED)
    if sportybet_home:
        return HOME_TOTAL_ID, lines.home_totals
    return AWAY_TOTAL_ID, lines.away_totals


def team_lines(lines: EventLines, orientation: str) -> tuple[Line | None, Line | None]:
    """The main team-total line for ESPN's home and away teams."""
    return (
        main_line(_team_ladder(lines, "home", orientation)[1]),
        main_line(_team_ladder(lines, "away", orientation)[1]),
    )


def team_ladders(lines: EventLines, orientation: str) -> tuple[tuple[Line, ...], tuple[Line, ...]]:
    """ESPN's home and away teams' team-total ladders, sorted by line."""
    return (
        _team_ladder(lines, "home", orientation)[1],
        _team_ladder(lines, "away", orientation)[1],
    )


def _leg(
    kind: Kind,
    prediction: GamePrediction,
    lines: EventLines,
    names: tuple[str, str],
    game_id: str,
    mode: str,
    team: Literal["home", "away"] | None,
    direction: Direction,
    projection: float,
    ladder: tuple[Line, ...],
    min_cushion: float,
) -> Leg | None:
    main = main_line(ladder)
    booked = booked_line(ladder, mode, direction, projection, min_cushion)
    if main is None or booked is None:
        return None
    outcome_id, odds = booked.outcome(direction)
    return Leg(
        kind=kind,
        event_id=lines.event_id,
        game_id=game_id,
        home=names[0],
        away=names[1],
        kickoff=lines.start,
        mode=mode,
        team=team,
        direction=direction,
        projection=projection,
        market_id=booked.market_id,
        specifier=booked.specifier,
        outcome_id=outcome_id,
        odds=odds,
        booked_line=booked.line,
        main_line=main.line,
        main_odds=main.outcome(direction)[1],
        edge_main=projection - main.line,
        edge_booked=projection - booked.line,
        flags=prediction.flags,
    )


def game_legs(
    prediction: GamePrediction,
    lines: EventLines,
    orientation: str,
    names: tuple[str, str],
    game_id: str,
    config: SportConfig,
) -> dict[Kind, Leg]:
    """At most one leg per kind for this game (D10). Ineligible games give none."""
    if not prediction.eligible:
        return {}
    legs: dict[Kind, Leg | None] = {}
    common = (prediction, lines, names, game_id)
    if prediction.lean in ("OVER", "UNDER"):
        direction: Direction = "Over" if prediction.lean == "OVER" else "Under"
        legs["TOTALS_LEAN"] = _leg(
            "TOTALS_LEAN", *common, config.line_mode.totals, None, direction,
            prediction.model_total, lines.totals, config.min_cushion,
        )  # fmt: skip
    pick = prediction.team_pick
    if pick is not None:
        _, ladder = _team_ladder(lines, pick.side, orientation)
        legs["TEAM_TOTALS"] = _leg(
            "TEAM_TOTALS", *common, config.line_mode.team_totals, pick.side, pick.direction,
            pick.proj, ladder, config.min_cushion,
        )  # fmt: skip
    legs["TOTALS_ALL_OVER"] = _leg(
        "TOTALS_ALL_OVER", *common, SAFEST, None, "Over", prediction.model_total, lines.totals,
        config.min_cushion,
    )  # fmt: skip
    legs["TOTALS_ALL_UNDER"] = _leg(
        "TOTALS_ALL_UNDER", *common, SAFEST, None, "Under", prediction.model_total, lines.totals,
        config.min_cushion,
    )  # fmt: skip
    return {kind: leg for kind, leg in legs.items() if leg is not None}


def recheck(
    leg: Leg,
    fresh: EventLines,
    config: SportConfig,
    now: datetime,
    cutoff: timedelta,
) -> tuple[Leg | None, str | None]:
    """Re-select the main and booked lines from a fresh ladder and re-decide the leg (brief §2.2,
    §2.3). Returns the leg as it would now be booked, or None and why it was dropped."""
    if not fresh.not_started(now):
        return None, "started"
    if fresh.start <= now + cutoff:
        return None, "inside_cutoff"
    if leg.team is None:
        ladder = fresh.totals
    elif leg.market_id == HOME_TOTAL_ID:
        ladder = fresh.home_totals
    elif leg.market_id == AWAY_TOTAL_ID:
        ladder = fresh.away_totals
    else:
        raise ValueError(f"Leg on unexpected market {leg.market_id}")
    main = main_line(ladder)
    if main is None:
        return None, "no_open_line"
    edge = leg.projection - main.line
    if leg.kind == "TOTALS_LEAN":
        wanted = "OVER" if leg.direction == "Over" else "UNDER"
        if lean(edge, config) != wanted:
            return None, "lean_gone"
    elif leg.kind == "TEAM_TOTALS":
        agrees = edge > 0 if leg.direction == "Over" else edge < 0
        if not agrees or abs(edge) < config.team_total_threshold - 1e-9:
            return None, "team_edge_gone"
    booked = booked_line(ladder, leg.mode, leg.direction, leg.projection, config.min_cushion)
    if booked is None:
        return None, "no_line_for_mode"
    outcome_id, odds = booked.outcome(leg.direction)
    return (
        replace(
            leg,
            kickoff=fresh.start,
            market_id=booked.market_id,
            specifier=booked.specifier,
            outcome_id=outcome_id,
            odds=odds,
            booked_line=booked.line,
            main_line=main.line,
            main_odds=main.outcome(leg.direction)[1],
            edge_main=edge,
            edge_booked=leg.projection - booked.line,
        ),
        None,
    )
