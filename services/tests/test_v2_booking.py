"""B3: who may book (D83, D97), real codes and their verification, superseding and rebooking."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
import respx
from moto import mock_aws

from hoopsedge.common.booking_mode import is_dry_run
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.common.http import PoliteClient
from hoopsedge.sportybet.client import ENDPOINTS, SportyBetClient
from hoopsedge.v2.booking import (
    BOOKED,
    DRY_RUN,
    FAILED,
    INVALID,
    NOT_BOOKED,
    SUPERSEDED,
    Booker,
    book_ticket,
    leg_document,
    leg_from_document,
    read_index,
    record_run,
)
from hoopsedge.v2.legs import Leg
from hoopsedge.v2.lines import parse_event
from hoopsedge.v2.slate import Runner, final_recheck, rebook
from hoopsedge.v2.store import settings_dry_run
from hoopsedge.v2.tickets import Ticket
from tests.test_v2_slate import CONFIG, KICKOFF, NCAAF, NOW, ladder_event, lean_leg, lookup

BASE = ENDPOINTS["base_url"]
SHARE = f"{BASE}{ENDPOINTS['share_create']['path']}"
DETAIL = f"{BASE}{ENDPOINTS['event_detail']['path']}"
CUTOFF = timedelta(minutes=20)


# Who may book (D83, D97).


@pytest.mark.parametrize(
    ("switch", "env", "dry"),
    [(True, "false", True), (False, "true", True), (False, None, True), (False, "false", False),
     (False, " FALSE ", False)],
)  # fmt: skip
def test_real_codes_need_both_switches_off(
    monkeypatch: pytest.MonkeyPatch, switch: bool, env: str | None, dry: bool
) -> None:
    if env is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", env)
    assert is_dry_run(switch) is dry


def test_the_trigger_no_longer_matters(monkeypatch: pytest.MonkeyPatch) -> None:
    """D83 replaced D78: a scheduled run on the owner's machine follows the same rule."""
    monkeypatch.setenv("HOOPSEDGE_ENV", "local")
    monkeypatch.setenv("DRY_RUN", "false")
    assert is_dry_run(False) is False


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource(
            "dynamodb", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="x",  # noqa: S106 - Dummy values.
        )  # fmt: skip
        ensure_table(resource, "t")
        yield resource.Table("t")


def test_the_dashboard_switch_defaults_to_dry_run(table: Any) -> None:
    assert settings_dry_run(table) is True
    table.put_item(Item={"pk": "CONFIG", "sk": "CURRENT", "document": {"dry_run": False}})
    assert settings_dry_run(table) is False
    table.put_item(Item={"pk": "CONFIG", "sk": "CURRENT", "document": {}})
    assert settings_dry_run(table) is True


# Booking one ticket.


def legs(count: int = 2, start: Any = KICKOFF, gap: timedelta = timedelta(minutes=1)) -> list[Leg]:
    return [
        lean_leg(parse_event(ladder_event([50.5], main=50.5, event_id=f"sr:match:{i}",
                                          start=start + i * gap)))
        for i in range(count)
    ]  # fmt: skip


def ticket(count: int = 2) -> Ticket:
    return Ticket("TOTALS_LEAN", 1, tuple(legs(count)))


def keep_all(given: Any) -> Any:
    return list(given), []


class Alerts:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str, str]] = []

    def __call__(self, severity: str, key: str, message: str) -> None:
        self.raised.append((severity, key, message))


def client() -> SportyBetClient:
    return SportyBetClient(
        PoliteClient("t", rate_per_second=1000, max_retries=0, sleep=lambda _: None)
    )


def mock_share(code: str, booked: list[dict[str, str]]) -> respx.Route:
    route = respx.post(SHARE).mock(
        return_value=httpx.Response(200, json={"bizCode": 10000, "data": {"shareCode": code}})
    )
    respx.get(f"{BASE}/orders/share/{code}").mock(
        return_value=httpx.Response(200, json=lookup(booked))
    )
    return route


@respx.mock
def test_dry_run_never_calls_sportybet() -> None:
    alerts = Alerts()
    result = book_ticket(Booker(None, False, alerts), ticket(), "01ABCDEFGHJK", keep_all, 2)
    assert (result.status, result.code) == (DRY_RUN, "DRYRUN-EFGHJK")
    assert not respx.calls and not alerts.raised


@respx.mock
def test_a_real_code_is_one_post_then_verified() -> None:
    wanted = ticket()
    route = mock_share("BK123", wanted.selections())
    result = book_ticket(Booker(client(), True, Alerts()), wanted, "t1", keep_all, 2)
    assert (result.status, result.code) == (BOOKED, "BK123")
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == {"selections": wanted.selections()}


@respx.mock
def test_a_code_that_reads_back_wrong_is_invalid_alerts_and_stops_the_run() -> None:
    wanted = ticket(3)
    route = mock_share("BAD1", wanted.selections()[:2])  # One leg lost.
    alerts = Alerts()
    booker = Booker(client(), True, alerts)
    result = book_ticket(booker, wanted, "t1", keep_all, 2)
    assert (result.status, result.code) == (INVALID, "BAD1")
    assert result.reason == "2 legs in the code, 3 intended"
    assert alerts.raised[0][:2] == ("CRITICAL", "code-invalid")
    later = book_ticket(booker, ticket(), "t2", keep_all, 2)
    assert later.status == NOT_BOOKED and later.reason and later.reason.startswith("stopped")
    assert route.call_count == 1  # Nothing more was booked after the bad code.


@respx.mock
def test_a_refused_or_blocked_booking_fails_loudly_once() -> None:
    route = respx.post(SHARE).mock(return_value=httpx.Response(403, text="denied"))
    alerts = Alerts()
    booker = Booker(client(), True, alerts)
    result = book_ticket(booker, ticket(), "t1", keep_all, 2)
    assert result.status == FAILED and result.code is None
    assert route.call_count == 1
    assert alerts.raised[0][1] == "booking-failed"
    assert booker.stopped


@respx.mock
def test_a_read_back_that_fails_makes_the_code_invalid() -> None:
    respx.post(SHARE).mock(
        return_value=httpx.Response(200, json={"bizCode": 10000, "data": {"shareCode": "C1"}})
    )
    respx.get(f"{BASE}/orders/share/C1").mock(return_value=httpx.Response(503))
    result = book_ticket(Booker(client(), True, Alerts()), ticket(), "t1", keep_all, 2)
    assert result.status == INVALID
    assert result.reason and result.reason.startswith("read-back failed")


@respx.mock
def test_the_final_recheck_can_leave_too_few_legs_to_book() -> None:
    def drop_one(given: Any) -> Any:
        kept = list(given)[1:]
        return kept, []

    result = book_ticket(Booker(client(), True, Alerts()), ticket(2), "t1", drop_one, 2)
    assert result.status == NOT_BOOKED and result.ticket is None
    assert not respx.calls


def test_live_booking_needs_a_client() -> None:
    with pytest.raises(ValueError):
        Booker(None, True, Alerts())


def test_legs_survive_a_round_trip_through_run_json() -> None:
    leg = legs(1)[0]
    stored = json.loads(json.dumps(leg_document(leg)))
    assert leg_from_document(stored) == leg


# The slate index (invariant 6, D97).


def entry(ticket_id: str, kind: str, status: str) -> dict[str, Any]:
    return {"ticket_id": ticket_id, "kind": kind, "number": 1, "status": status, "code": "c"}


def test_real_codes_supersede_earlier_real_codes_of_the_same_kind(tmp_path: Path) -> None:
    record_run(tmp_path, "r1", "scheduled",
               [entry("a", "TOTALS_LEAN", BOOKED), entry("b", "TEAM_TOTALS", BOOKED)])  # fmt: skip
    record_run(tmp_path, "r2", "scheduled", [entry("c", "TOTALS_LEAN", DRY_RUN)])
    assert read_index(tmp_path)["tickets"]["a"]["status"] == BOOKED  # Practice codes don't.
    record_run(tmp_path, "r3", "scheduled", [entry("d", "TOTALS_LEAN", BOOKED)])
    tickets = read_index(tmp_path)["tickets"]
    assert tickets["a"]["status"] == SUPERSEDED and tickets["a"]["superseded_by"] == "r3"
    assert tickets["b"]["status"] == BOOKED  # Another kind.
    assert tickets["d"]["status"] == BOOKED
    assert [run["run_id"] for run in read_index(tmp_path)["runs"]] == ["r1", "r2", "r3"]


# Rebooking (brief §7).


class FakeSportyBet:
    """Event details by ID, as SportyBet would return them now."""

    def __init__(self, events: dict[str, dict[str, Any]]) -> None:
        self.events = events
        self.asked: list[str] = []

    def event_detail(self, event_id: str) -> dict[str, Any]:
        self.asked.append(event_id)
        return {"data": self.events[event_id]}


def test_rebook_drops_started_legs_and_links_both_tickets(tmp_path: Path) -> None:
    slate_dir = tmp_path / "2026-11-28"
    old_legs = legs(3, start=NOW + timedelta(hours=1), gap=timedelta(hours=1))
    old_run = slate_dir / "r1"
    old_run.mkdir(parents=True)
    stored = {
        "tickets": [{**entry("old", "TOTALS_LEAN", DRY_RUN),
                     "leg_details": [leg_document(leg) for leg in old_legs]}]
    }  # fmt: skip
    (old_run / "run.json").write_text(json.dumps(stored), "utf-8")
    record_run(slate_dir, "r1", "scheduled", stored["tickets"])
    before = (old_run / "run.json").read_text("utf-8")

    now = NOW + timedelta(hours=1, minutes=5)  # The first game kicked off 5 minutes ago.
    events = {
        leg.event_id: ladder_event(
            [50.5], main=50.5, event_id=leg.event_id, start=leg.kickoff,
            match_status="1st half" if leg.kickoff < now else "Not start",
        )
        for leg in old_legs
    }  # fmt: skip
    fake = FakeSportyBet(events)
    runner = Runner(fake, tmp_path, clock=lambda: now)  # type: ignore[arg-type]
    document = rebook(
        runner, slate_dir, "old", CONFIG, "NCAAF", Booker(None, False, Alerts()), slate_dir / "r2"
    )
    [new] = document["tickets"]
    assert new["status"] == DRY_RUN and new["rebook_of"] == "old"
    assert new["legs"] == 2
    assert [d["reason"] for d in new["dropped_at_booking"]] == ["started"]
    assert sorted(fake.asked) == sorted(leg.event_id for leg in old_legs)  # All re-fetched.
    index = read_index(slate_dir)["tickets"]
    assert index["old"]["rebooked_by"] == new["ticket_id"]
    assert index[new["ticket_id"]]["rebook_of"] == "old"
    assert (old_run / "run.json").read_text("utf-8") == before  # Never mutated.


def test_rebook_of_an_unknown_ticket_fails(tmp_path: Path) -> None:
    runner = Runner(FakeSportyBet({}), tmp_path, clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="No ticket"):
        rebook(runner, tmp_path, "nope", CONFIG, "NCAAF", Booker(None, False, Alerts()),
               tmp_path / "r")  # fmt: skip


def test_final_recheck_uses_lines_no_older_than_five_minutes(tmp_path: Path) -> None:
    leg = legs(1)[0]
    fake = FakeSportyBet({leg.event_id: ladder_event([50.5], main=50.5, event_id=leg.event_id)})
    clock = {"now": NOW}
    runner = Runner(fake, tmp_path, clock=lambda: clock["now"])  # type: ignore[arg-type]
    runner.detail(leg.event_id)
    final_recheck(runner, NCAAF, CUTOFF)([leg])
    assert fake.asked == [leg.event_id]  # Seconds old: reused.
    clock["now"] = NOW + timedelta(minutes=6)
    kept, dropped = final_recheck(runner, NCAAF, CUTOFF)([leg])
    assert kept and not dropped
    assert fake.asked == [leg.event_id, leg.event_id]  # Six minutes old: fetched again.
