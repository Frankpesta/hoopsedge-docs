"""What changed between two builds, and repricing a ticket (docs/07 section 6.6, D35).

The re-check is a full rebuild from fresh offers, so a moved line or price is re-scored by the same
rules as everything else. These functions say what that changed, for the run log.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from hoopsedge.engine.build import selection_hash
from hoopsedge.engine.schemas import NOT_STARTED, BuildResult, Leg, Offer, Ticket
from hoopsedge.markets.probability import expected_value, fair_probability

LEG_DROPPED = "LEG_DROPPED"
LEG_ADDED = "LEG_ADDED"
LINE_CHANGED = "LINE_CHANGED"
ODDS_MOVED = "ODDS_MOVED"
REJECTED_AT_BOOKING = "REJECTED_AT_BOOKING"


@dataclass(frozen=True)
class Change:
    kind: str
    game_id: str
    family: str
    detail: str


def _legs(result: BuildResult) -> dict[tuple[str, str, str | None], Leg]:
    return {
        (leg.game_id, leg.family, leg.team): leg for ticket in result.tickets for leg in ticket.legs
    }


def compare_builds(before: BuildResult, after: BuildResult) -> list[Change]:
    old, new = _legs(before), _legs(after)
    changes: list[Change] = []
    for key in sorted(old.keys() | new.keys(), key=str):
        was, now = old.get(key), new.get(key)
        if was is None and now is not None:
            changes.append(Change(LEG_ADDED, now.game_id, now.family, now.specifier))
        elif was is not None and now is None:
            changes.append(Change(LEG_DROPPED, was.game_id, was.family, was.specifier))
        elif was is not None and now is not None:
            if (was.specifier, was.outcome_id) != (now.specifier, now.outcome_id):
                detail = f"{was.specifier} {was.side} to {now.specifier} {now.side}"
                changes.append(Change(LINE_CHANGED, now.game_id, now.family, detail))
            elif was.odds != now.odds:
                changes.append(
                    Change(ODDS_MOVED, now.game_id, now.family, f"{was.odds} to {now.odds}")
                )
    return changes


def reprice_leg(leg: Leg, offer: Offer) -> Leg:
    """The same selection at the offer's current prices."""
    mine = next(item for item in offer.outcomes if item.outcome_id == leg.outcome_id)
    other = next(item for item in offer.outcomes if item.outcome_id != leg.outcome_id)
    fair = fair_probability(mine.odds, other.odds)
    # D79: the lean toward the market follows the new fair price.
    model = leg.p_calibrated if leg.p_model is None else leg.p_model
    chance = fair + leg.model_weight * (model - fair)
    settled = 1.0 - leg.p_push
    p_win, p_lose = chance * settled, (1.0 - chance) * settled
    return leg.model_copy(
        update={
            "odds": mine.odds,
            "fair": fair,
            "p_calibrated": chance,
            "p_win": p_win,
            "p_lose": p_lose,
            "edge": chance - fair,
            "ev": expected_value(p_win, p_lose, mine.odds),
            "offer_fetched_at": offer.fetched_at,
        }
    )


def rebuild_ticket(ticket: Ticket, legs: Sequence[Leg]) -> Ticket:
    """A ticket with fewer legs or new prices. Its legs keep their ranks."""
    odds = math.prod(leg.odds for leg in legs)
    chance = math.prod(leg.p_win for leg in legs)
    return ticket.model_copy(
        update={
            "legs": tuple(legs),
            "combined_odds": odds,
            "combined_prob": chance,
            "expected_value": chance * odds - 1.0,
            "selection_hash": selection_hash(legs),
        }
    )


def still_bookable(
    ticket: Ticket,
    offers: Mapping[tuple[str, str, str], Offer],
    min_edge: float,
    earliest_tip: datetime,
) -> tuple[list[Leg], list[Leg]]:
    """D35: SportyBet names no bad leg, so ask the fresh offers.

    Kept legs get fresh prices, and invariants 2 and 3 still apply: a leg now too close to tip, or
    whose new price leaves too little edge, is dropped too.
    """
    kept: list[Leg] = []
    dropped: list[Leg] = []
    for leg in ticket.legs:
        offer = offers.get((leg.event_id, leg.market_id, leg.specifier))
        if (
            offer is None
            or not offer.bookable
            or offer.status != NOT_STARTED
            or offer.tip_time < earliest_tip
        ):
            dropped.append(leg)
            continue
        fresh = reprice_leg(leg, offer)
        (kept if fresh.edge >= min_edge else dropped).append(fresh)
    return kept, dropped
