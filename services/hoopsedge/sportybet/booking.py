"""Creating booking codes (docs/07 section 6.4, invariant 8, C12).

A code is a shareable slip and nothing more: this package can't stake anything on it (D11). Booking
happens only if both the environment and the config allow it, so either can force dry-run. A local
environment never books from here: the single approved test has its own command and ledger.
"""

import os
from collections.abc import Sequence

from hoopsedge.common.dynamo import LOCAL_ENV
from hoopsedge.engine.schemas import Leg
from hoopsedge.sportybet.client import SportyBetClient

DRY_RUN_PREFIX = "DRYRUN-"


def is_dry_run(config_dry_run: bool = False) -> bool:
    if config_dry_run or os.environ.get("HOOPSEDGE_ENV", LOCAL_ENV) == LOCAL_ENV:
        return True
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


def selections(legs: Sequence[Leg]) -> list[dict[str, str]]:
    """Exactly as Phase 0 recorded it. Ordered by rank, so a repeat gives the same payload."""
    return [
        {
            "eventId": leg.event_id,
            "marketId": leg.market_id,
            "specifier": leg.specifier,
            "outcomeId": leg.outcome_id,
        }
        for leg in sorted(legs, key=lambda item: item.rank)
    ]


def dry_run_code(ticket_id: str) -> str:
    """The end of the ID, not docs/07's start: a ULID starts with its time, so all would match."""
    return f"{DRY_RUN_PREFIX}{ticket_id[-6:]}"


def create_code(client: SportyBetClient, legs: Sequence[Leg]) -> str:
    return client.create_code(selections(legs))
