"""Settlement rules (docs/08 section 7.1). Pure, so every case can be tested by hand.

`home` means the canonical home team: legs are stored that way even if SportyBet listed the game
the other way round (D73). The final score includes overtime, which is what every full-game market
settles on (`markets.yaml`, D31). A first-half total can't be touched by overtime.
"""

from collections.abc import Iterable
from dataclasses import dataclass

WON = "won"
LOST = "lost"
PUSH = "push"
VOID = "void"
PENDING = "pending"
SETTLED_AT_ONE = frozenset({PUSH, VOID})


@dataclass(frozen=True)
class Final:
    home: int
    away: int
    home_1h: int | None
    away_1h: int | None


@dataclass(frozen=True)
class Settlement:
    status: str
    actual_value: int  # Margin for a handicap, otherwise the total the line was set on.


def _first_half(final: Final) -> int:
    if final.home_1h is None or final.away_1h is None:
        raise ValueError("No first-half score to settle on")
    return final.home_1h + final.away_1h


def settle_leg(family: str, line: float, team: str | None, side: str, final: Final) -> Settlement:
    if family == "handicap":
        actual = final.home - final.away
        difference = actual + line  # Home -5.5 needs a margin above 5.5.
        wins_when_above = side == "home"
    else:
        if family == "total_ft":
            actual = final.home + final.away
        elif family == "total_1h":
            actual = _first_half(final)
        elif team == "home":
            actual = final.home
        elif team == "away":
            actual = final.away
        else:
            raise ValueError(f"Can't settle {family} for team {team}")
        difference = actual - line
        wins_when_above = side == "over"
    if difference == 0:
        return Settlement(PUSH, actual)
    return Settlement(WON if (difference > 0) == wins_when_above else LOST, actual)


def settle_ticket(legs: Iterable[tuple[str, float]]) -> tuple[str, float]:
    """The ticket's status, and the product of the odds settled so far (status, odds)."""
    odds = 1.0
    statuses = set()
    for status, price in legs:
        statuses.add(status)
        if status == WON:
            odds *= price
    if LOST in statuses:
        return LOST, odds
    if PENDING in statuses:
        return PENDING, odds
    return WON, odds
