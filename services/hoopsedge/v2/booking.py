"""Booking codes for v2 tickets, verifying them, superseding and rebooking (brief §2, §7; D83, D97).

A code is a shareable slip and nothing more: nothing here can log in, stake or place a bet (D11).
Each ticket's legs are re-checked one last time against lines no more than 5 minutes old, then the
code is created with exactly one POST and read back. It counts as booked only if the read-back
holds exactly the intended selections (invariant 4, v1 strength). A refusal, a block, an
unreadable read-back or a mismatch raises an alert and stops every later real booking in the run.

Real codes need both switches off: the dashboard's "Dry run" and the container's `DRY_RUN` (D97).

Runs stay immutable. What changes afterwards (a later run's real codes superseding these, a
rebook replacing a ticket) is recorded in the slate's `index.json` beside the run folders.
"""

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from hoopsedge.sportybet.client import SportyBetClient, SportyBetError
from hoopsedge.v2.legs import Leg
from hoopsedge.v2.tickets import Dropped, Ticket, dry_run_code
from hoopsedge.v2.verify import CodeCheck, check_code

DRY_RUN = "dry_run"
BOOKED = "booked"
INVALID = "invalid"  # Created, but the read-back didn't match: never use it.
FAILED = "failed"  # SportyBet refused or blocked: no usable code.
NOT_BOOKED = "not_booked"
SUPERSEDED = "superseded"

Alert = Callable[[str, str, str], None]  # severity, key, message
Recheck = Callable[[Sequence[Leg]], tuple[list[Leg], list[Dropped]]]


@dataclass
class Booker:
    """One run's booking state. `live` is decided once, before anything is booked."""

    client: SportyBetClient | None
    live: bool
    alert: Alert
    stopped: str | None = None  # Why later real bookings were refused.

    def __post_init__(self) -> None:
        if self.live and self.client is None:
            raise ValueError("Live booking needs a SportyBet client")


@dataclass(frozen=True)
class Booked:
    ticket: Ticket | None  # As booked, after the final re-check; None if nothing was left.
    status: str
    code: str | None
    reason: str | None = None
    dropped: tuple[Dropped, ...] = ()
    missing: tuple[Any, ...] = ()
    extra: tuple[Any, ...] = ()


def book_ticket(
    booker: Booker, ticket: Ticket, ticket_id: str, recheck: Recheck, min_legs: int
) -> Booked:
    legs, dropped = recheck(ticket.legs)
    if len(legs) < min_legs:
        reason = f"{len(legs)} legs left after the final re-check, under {min_legs}"
        return Booked(None, NOT_BOOKED, None, reason, tuple(dropped))
    final = replace(ticket, legs=tuple(sorted(legs, key=lambda leg: (leg.kickoff, leg.event_id))))
    if not booker.live:
        return Booked(final, DRY_RUN, dry_run_code(ticket_id), None, tuple(dropped))
    if booker.stopped is not None:
        return Booked(final, NOT_BOOKED, None, f"stopped: {booker.stopped}", tuple(dropped))
    client = booker.client
    assert client is not None  # noqa: S101 - Guaranteed by Booker.__post_init__.
    selections = final.selections()
    try:
        code = client.create_code(selections)  # Exactly one POST, never retried.
    except SportyBetError as error:
        reason = f"{type(error).__name__}: {error}"
        booker.stopped = f"ticket {ticket_id} failed to book"
        booker.alert("CRITICAL", "booking-failed", f"{final.kind} #{final.number}: {reason}")
        return Booked(final, FAILED, None, reason, tuple(dropped))
    try:
        check = check_code(selections, client.lookup(code))
    except SportyBetError as error:
        check = CodeCheck(False, f"read-back failed: {type(error).__name__}: {error}")
    if not check.verified:
        booker.stopped = f"code {code} didn't verify"
        message = f"{code} ({final.kind} #{final.number}): {check.reason}"
        booker.alert("CRITICAL", "code-invalid", message)
        return Booked(
            final, INVALID, code, check.reason, tuple(dropped), check.missing, check.extra
        )
    return Booked(final, BOOKED, code, None, tuple(dropped))


# Turning stored legs back into legs, for rebooking.


def leg_from_document(document: dict[str, Any]) -> Leg:
    values = dict(document)
    values["kickoff"] = datetime.fromisoformat(str(values["kickoff"]))
    values["flags"] = tuple(values.get("flags") or ())
    return Leg(**values)


def leg_document(leg: Leg) -> dict[str, Any]:
    data = asdict(leg)
    data["kickoff"] = leg.kickoff.isoformat()
    data["flags"] = list(leg.flags)
    return data


# The slate index: what superseded what (invariant 6).


def index_path(slate_dir: Path) -> Path:
    return slate_dir / "index.json"


def read_index(slate_dir: Path) -> dict[str, Any]:
    path = index_path(slate_dir)
    if path.exists():
        index: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return index
    return {"runs": [], "tickets": {}}


def _write_index(slate_dir: Path, index: dict[str, Any]) -> None:
    slate_dir.mkdir(parents=True, exist_ok=True)
    path = index_path(slate_dir)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(index, indent=1, sort_keys=True), "utf-8")
    os.replace(temporary, path)


def record_run(
    slate_dir: Path,
    run_id: str,
    trigger: str,
    tickets: Iterable[dict[str, Any]],
    rebook_of: str | None = None,
) -> dict[str, Any]:
    """Add a run's tickets. Real codes booked now supersede earlier real codes of the same kind
    (owner, D97); a rebook replaces only the ticket it rebooked. Nothing is ever removed."""
    index = read_index(slate_dir)
    index["runs"].append({"run_id": run_id, "trigger": trigger})
    new = list(tickets)
    booked_kinds = {t["kind"] for t in new if t["status"] == BOOKED}
    for ticket_id, entry in index["tickets"].items():
        if rebook_of is not None:
            if ticket_id == rebook_of and new:
                entry["rebooked_by"] = new[0]["ticket_id"]
                if entry["status"] == BOOKED and new[0]["status"] == BOOKED:
                    entry["status"], entry["superseded_by"] = SUPERSEDED, run_id
        elif entry["status"] == BOOKED and entry["kind"] in booked_kinds:
            entry["status"], entry["superseded_by"] = SUPERSEDED, run_id
    for ticket in new:
        index["tickets"][ticket["ticket_id"]] = {
            "run_id": run_id,
            "kind": ticket["kind"],
            "number": ticket["number"],
            "status": ticket["status"],
            "code": ticket["code"],
            **({"rebook_of": rebook_of} if rebook_of else {}),
        }
    _write_index(slate_dir, index)
    return index
