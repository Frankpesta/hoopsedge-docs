"""The one owner-approved live booking of docs/07's Definition of Done.

    docker compose run --rm pipeline python -m hoopsedge.pipeline.live_test plan --run <run_id>
    docker compose run --rm -e DRY_RUN=false pipeline \
        python -m hoopsedge.pipeline.live_test book --run <run_id> --confirm <hash>

`plan` books nothing. `book` makes exactly one POST for the two legs `plan` showed, re-checked
against fresh offers. It needs the DRY_RUN override, the hash `plan` printed, and a free slot in
the Phase 0 test-code ledger, which is capped at 5 codes.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hoopsedge.engine.build import selection_hash
from hoopsedge.engine.config import load_engine_config
from hoopsedge.engine.recheck import rebuild_ticket, still_bookable
from hoopsedge.engine.schemas import EngineConfig, Leg, Ticket
from hoopsedge.sportybet.booking import selections
from hoopsedge.sportybet.client import SportyBetClient, build_client
from hoopsedge.sportybet.discovery import assert_code_available
from hoopsedge.sportybet.offers import fetch_snapshot, normalize_event
from hoopsedge.sportybet.verification import verify_code

LEGS = 2
DEFAULT_DATA_DIR = "/workspace/data"
LEDGER = Path("/workspace/docs/reports/phase0-sportybet-test-codes.json")


def choose_ticket(run_document: dict[str, Any]) -> Ticket:
    """The first two legs of the first ticket in a dry run: real, ranked, different games."""
    tickets = run_document["final_build"]["tickets"]
    if not tickets:
        raise SystemExit("That run built no tickets, so there's nothing to book.")
    ticket = Ticket.model_validate(tickets[0])
    if len(ticket.legs) < LEGS:
        raise SystemExit("That run's first ticket has fewer than two legs.")
    return rebuild_ticket(ticket, ticket.legs[:LEGS])


def _read_run(data_dir: Path, run_id: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (data_dir / "runs" / run_id / "result.json").read_text("utf-8")
    )
    return document


def fresh_legs(
    client: SportyBetClient, ticket: Ticket, config: EngineConfig, now: datetime
) -> list[Leg]:
    """Fresh prices for the same selections. Any leg that no longer qualifies stops the booking."""
    snapshot = fetch_snapshot(client, ticket.league, now)
    offers = {
        (offer.event_id, offer.market_id, offer.specifier): offer
        for event in snapshot.events
        if event.event_id in {leg.event_id for leg in ticket.legs}
        for offer in normalize_event(event, "live-test", None, now)
    }
    market = config.markets[ticket.league][ticket.family]
    assert market.min_edge is not None  # noqa: S101 - The ticket passed it to exist.
    earliest = now + timedelta(minutes=config.min_minutes_before_tip)
    kept, dropped = still_bookable(ticket, offers, market.min_edge, earliest)
    if dropped:
        names = [leg.specifier for leg in dropped]
        raise SystemExit(f"Refusing to book: {names} no longer qualify. Plan again.")
    return kept


def book(
    client: SportyBetClient,
    ticket: Ticket,
    config: EngineConfig,
    confirm: str,
    ledger: Path,
    now: datetime,
) -> dict[str, Any]:
    if os.environ.get("DRY_RUN", "true").strip().lower() != "false":
        raise SystemExit("Refusing to book: DRY_RUN must be false.")
    if len(ticket.legs) != LEGS:
        raise SystemExit("Refusing to book: the live test is exactly two legs.")
    if confirm != ticket.selection_hash[:12]:
        raise SystemExit("Refusing to book: the hash doesn't match what `plan` showed.")
    attempts: list[dict[str, Any]] = json.loads(ledger.read_text("utf-8"))
    assert_code_available(attempts)
    legs = fresh_legs(client, ticket, config, now)
    attempt: dict[str, Any] = {
        "name": "phase6-live-test",
        "legs": selections(legs),
        "at": now.isoformat(),
        "code": None,
        "outcome": "pending",
    }
    # Written before the POST, so that a crash can't hide a code from the cap.
    ledger.write_text(json.dumps([*attempts, attempt], indent=1) + "\n", "utf-8")
    code = client.create_code(selections(legs))
    attempt.update(code=code, outcome="created")
    ledger.write_text(json.dumps([*attempts, attempt], indent=1) + "\n", "utf-8")
    result = verify_code(legs, client.lookup(code))
    attempt.update(
        verified=result.verified,
        difference={"missing": result.missing, "extra": result.extra},
        odds_differences=result.odds_differences,
    )
    ledger.write_text(json.dumps([*attempts, attempt], indent=1, default=list) + "\n", "utf-8")
    return attempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "book"])
    parser.add_argument("--run", required=True)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    ticket = choose_ticket(_read_run(data_dir, args.run))
    if args.command == "plan":
        print(
            json.dumps(
                {
                    "league": ticket.league,
                    "family": ticket.family,
                    "confirm": ticket.selection_hash[:12],
                    "legs": [
                        {
                            "game_id": leg.game_id,
                            "event_id": leg.event_id,
                            "specifier": leg.specifier,
                            "side": leg.side,
                            "team": leg.team,
                            "odds": leg.odds,
                            "p_calibrated": round(leg.p_calibrated, 4),
                            "tip_time": leg.tip_time.isoformat(),
                        }
                        for leg in ticket.legs
                    ],
                    "selection_hash_check": selection_hash(ticket.legs) == ticket.selection_hash,
                },
                indent=1,
            )
        )
        return 0
    config = load_engine_config({})
    client = build_client(data_dir / f"raw/sportybet/live-test-{args.run}")
    attempt = book(client, ticket, config, args.confirm or "", LEDGER, datetime.now(UTC))
    print(json.dumps(attempt, indent=1, default=list))
    return 0 if attempt.get("verified") else 1


if __name__ == "__main__":
    sys.exit(main())
