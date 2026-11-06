"""Every code is read back and compared with what we meant to book (invariant 4, docs/07).

Pure: the lookup is fetched by the caller. Anything but an exact match of the selections is not
verified, including a response we can't read.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hoopsedge.engine.schemas import Leg
from hoopsedge.sportybet.discovery import SelectionKey, decoded_selections

ODDS_TOLERANCE = 0.02


@dataclass(frozen=True)
class Verification:
    verified: bool
    reason: str | None
    missing: tuple[SelectionKey, ...] = ()
    extra: tuple[SelectionKey, ...] = ()
    odds_differences: tuple[tuple[SelectionKey, float, float], ...] = ()  # Ours, then theirs.


def leg_key(leg: Leg) -> SelectionKey:
    return (leg.event_id, leg.market_id, leg.specifier or None, leg.outcome_id)


def _decoded_odds(document: dict[str, Any]) -> dict[SelectionKey, float]:
    found: dict[SelectionKey, float] = {}
    for event in document["data"]["outcomes"]:
        for market in event["markets"]:
            for outcome in market["outcomes"]:
                if outcome.get("odds") is not None:
                    key = (
                        str(event["eventId"]),
                        str(market["id"]),
                        market.get("specifier") or None,
                        str(outcome["id"]),
                    )
                    found[key] = float(outcome["odds"])
    return found


def verify_code(legs: Sequence[Leg], document: dict[str, Any]) -> Verification:
    try:
        found = decoded_selections(document)
        decoded_odds = _decoded_odds(document)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        return Verification(False, f"unreadable lookup: {type(error).__name__}: {error}")
    wanted = {leg_key(leg) for leg in legs}
    missing = tuple(sorted(wanted - found, key=str))
    extra = tuple(sorted(found - wanted, key=str))
    differences = tuple(
        (leg_key(leg), leg.odds, decoded_odds[leg_key(leg)])
        for leg in legs
        if leg_key(leg) in decoded_odds
        and round(abs(decoded_odds[leg_key(leg)] - leg.odds), 6) > ODDS_TOLERANCE
    )
    if missing or extra:
        reason = f"{len(missing)} selections missing, {len(extra)} unexpected"
        return Verification(False, reason, missing, extra, differences)
    return Verification(True, None, (), (), differences)
