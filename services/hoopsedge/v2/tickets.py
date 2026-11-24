"""Legs to tickets (brief §7): cutoff, top 100 by edge, split by kickoff into tickets of 20.

Pure. Legs below threshold never got this far, and nothing pads a ticket: a last split shorter
than `min_legs` is dropped, with its legs recorded (D4).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from hoopsedge.common.config_v2 import TicketConfig
from hoopsedge.v2.legs import Kind, Leg

DRY_RUN_PREFIX = "DRYRUN-"


def dry_run_code(ticket_id: str) -> str:
    """The end of the ID, not its start: a ULID starts with its time, so all would look alike."""
    return f"{DRY_RUN_PREFIX}{ticket_id[-6:]}"


@dataclass(frozen=True)
class Ticket:
    kind: Kind
    number: int  # 1 is the earliest-starting ticket of this kind.
    legs: tuple[Leg, ...]

    @property
    def total_odds(self) -> float:
        return round(math.prod(leg.odds for leg in self.legs), 2)

    @property
    def bookmaker_implied(self) -> float:
        """Product of 1/odds. Labelled bookmaker-implied in every view: the margin is inside."""
        return math.prod(1 / leg.odds for leg in self.legs)

    @property
    def earliest_kickoff(self) -> datetime:
        return min(leg.kickoff for leg in self.legs)

    @property
    def model_disagrees(self) -> int:
        """Legs whose projection is on the wrong side of the booked line (brief §7.3)."""
        return sum(leg.cushion < 0 for leg in self.legs)

    def selections(self) -> list[dict[str, str]]:
        """The booking payload, in ticket order (kickoff)."""
        return [
            {
                "eventId": leg.event_id,
                "marketId": leg.market_id,
                "specifier": leg.specifier,
                "outcomeId": leg.outcome_id,
            }
            for leg in self.legs
        ]


@dataclass(frozen=True)
class Dropped:
    leg: Leg
    reason: str


def too_late(leg: Leg, now: datetime, cutoff: timedelta) -> bool:
    return leg.kickoff <= now + cutoff


def build_tickets(
    kind: Kind, legs: Sequence[Leg], config: TicketConfig, now: datetime
) -> tuple[list[Ticket], list[Dropped]]:
    cutoff = timedelta(minutes=config.booking_cutoff_minutes)
    dropped = [Dropped(leg, "inside_cutoff") for leg in legs if too_late(leg, now, cutoff)]
    usable = [leg for leg in legs if not too_late(leg, now, cutoff)]
    if len({leg.event_id for leg in usable}) != len(usable):
        raise ValueError(f"{kind}: two legs on one game (D10)")
    ranked = sorted(usable, key=lambda leg: (-leg.rank, leg.kickoff, leg.event_id))
    kept = ranked[: config.max_games_per_kind]
    dropped += [Dropped(leg, "beyond_top_games") for leg in ranked[config.max_games_per_kind :]]
    ordered = sorted(kept, key=lambda leg: (leg.kickoff, leg.event_id))
    tickets: list[Ticket] = []
    for start in range(0, len(ordered), config.max_legs):
        chunk = tuple(ordered[start : start + config.max_legs])
        if len(chunk) < config.min_legs:
            dropped += [Dropped(leg, "ticket_below_min_legs") for leg in chunk]
            continue
        tickets.append(Ticket(kind, len(tickets) + 1, chunk))
    return tickets, dropped
