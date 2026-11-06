"""The whole slate pipeline on recorded offers, with every failure path (docs/00, docs/07)."""

import copy
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import boto3
import httpx
import numpy as np
import polars as pl
import pytest
import respx
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from moto import mock_aws

from hoopsedge.common.dynamo import ensure_table
from hoopsedge.common.http import PoliteClient
from hoopsedge.engine import InjuryStatus, Prediction, Reason
from hoopsedge.engine.recheck import (
    LEG_ADDED,
    LEG_DROPPED,
    LINE_CHANGED,
    ODDS_MOVED,
    compare_builds,
    rebuild_ticket,
    still_bookable,
)
from hoopsedge.engine.schemas import BuildResult
from hoopsedge.features.builder import FeatureRow
from hoopsedge.features.registry import FEATURE_SET_VERSION, FEATURES
from hoopsedge.ingest.backfill import write_gz
from hoopsedge.models.distribution import ScoreDistribution
from hoopsedge.pipeline import live_test
from hoopsedge.pipeline import predict as predict_module
from hoopsedge.pipeline.predict import (
    game_ref,
    history_seasons,
    injury_status,
    latest_version,
    predict_games,
)
from hoopsedge.pipeline.slate import LIVE_RUN_TIMES, SlateContext, SlateLocked, run_slate
from hoopsedge.pipeline.store import PipelineStore
from hoopsedge.ratings.history import GameRef, History, d1_teams
from hoopsedge.sportybet.client import ENDPOINTS, SportyBetClient
from hoopsedge.sportybet.offers import parse_event
from hoopsedge.teams.matcher import TeamIndex
from hoopsedge.teams.normalize import normalize_name
from hoopsedge.teams.registry import NON_D1
from tests.engine_data import config, period, prediction

FIXTURES = Path(__file__).parent / "fixtures/sportybet"
BASE = ENDPOINTS["base_url"]
EVENTS = f"{BASE}{ENDPOINTS['events']['path']}"
SHARE = f"{BASE}{ENDPOINTS['share_create']['path']}"
SLATE = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)  # Two hours before the first recorded NCAA tip.


def _events() -> list[dict[str, Any]]:
    document = json.loads((FIXTURES / "events-ncaa-page1.json").read_text("utf-8"))
    return copy.deepcopy(document["data"]["tournaments"][0]["events"])


class Sporty:
    """A fake SportyBet that remembers what was booked, and can be told to misbehave."""

    def __init__(self) -> None:
        self.events = _events()
        self.booked: list[list[dict[str, str]]] = []
        self.create_responses: list[Any] = []
        self.lookup_response: httpx.Response | None = None

    def list_events(self, _: httpx.Request) -> httpx.Response:
        body = {
            "bizCode": 10000,
            "data": {"totalNum": len(self.events), "tournaments": [{"events": self.events}]},
        }
        return httpx.Response(200, json=body)

    def create(self, request: httpx.Request) -> httpx.Response:
        self.booked.append(json.loads(request.content)["selections"])
        if self.create_responses:
            response = self.create_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json=response)
        code = f"CODE{len(self.booked)}"
        return httpx.Response(200, json={"bizCode": 10000, "data": {"shareCode": code}})

    def lookup(self, request: httpx.Request) -> httpx.Response:
        if self.lookup_response is not None:
            return self.lookup_response
        number = int(str(request.url).rsplit("CODE", 1)[1])
        outcomes = [
            {
                "eventId": item["eventId"],
                "markets": [
                    {
                        "id": item["marketId"],
                        "specifier": item["specifier"],
                        "outcomes": [{"id": item["outcomeId"]}],
                    }
                ],
            }
            for item in self.booked[number - 1]
        ]
        return httpx.Response(200, json={"bizCode": 10000, "data": {"outcomes": outcomes}})


def _team(name: str) -> str:
    return normalize_name(name).replace(" ", "-")


def _game_item(event: dict[str, Any], **changes: Any) -> dict[str, Any]:
    game_id = f"game-{event['eventId']}"
    tip = datetime.fromtimestamp(event["estimateStartTime"] / 1000, UTC)
    return {
        "pk": f"GAME#NCAA#{game_id}",
        "sk": "META",
        "gsi1pk": f"SLATE#NCAA#{SLATE}",
        "gsi1sk": tip.isoformat(),
        "league": "NCAA",
        "game_id": game_id,
        "season": 2027,
        "slate_date_et": SLATE.isoformat(),
        "tip_utc": tip.isoformat(),
        "home_id": _team(event["homeTeamName"]),
        "away_id": _team(event["awayTeamName"]),
        "neutral": False,
        "postseason": False,
        "status": "STATUS_SCHEDULED",
        **changes,
    }


def _predict(_: str, items: Sequence[dict[str, Any]], __: datetime) -> list[Prediction]:
    return [
        prediction(str(item["game_id"]), "NCAA", ft=period(120.0, 95.0), h1=period(60.0, 48.0))
        for item in items
    ]


class Harness:
    def __init__(self, tmp_path: Path, table: Any, sporty: Sporty) -> None:
        self.table = table
        self.sporty = sporty
        self.store = PipelineStore(table)
        self.clock = NOW
        self.tmp_path = tmp_path
        self.events = sporty.events
        for event in self.events:
            table.put_item(Item=_game_item(event))
        names = [name for e in self.events for name in (e["homeTeamName"], e["awayTeamName"])]
        http = PoliteClient("test", rate_per_second=1000, max_retries=0, sleep=lambda _: None)
        self.ctx = SlateContext(
            data_dir=tmp_path,
            store=self.store,
            client=SportyBetClient(http),
            config=config(),
            predict=_predict,
            refresh=lambda _: SimpleNamespace(failures={}),
            dry_run=True,
            now=lambda: self.clock,
            aliases={"NCAA": TeamIndex({normalize_name(n): _team(n) for n in names})},
        )

    def run(self, **changes: Any) -> Any:
        for name, value in changes.items():
            setattr(self.ctx, name, value)
        return run_slate(self.ctx, "NCAA", SLATE)

    def query(self, pk: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.table.query(KeyConditionExpression=Key("pk").eq(pk))[
            "Items"
        ]
        return items

    def alerts(self) -> list[dict[str, Any]]:
        return self.query("ALERT")

    def ticket_items(self) -> list[dict[str, Any]]:
        return self.store.slate_tickets("NCAA", SLATE)

    def legs_of(self, ticket_id: str) -> list[dict[str, Any]]:
        return [item for item in self.query(f"TICKET#{ticket_id}") if item["sk"].startswith("LEG#")]


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with mock_aws(), respx.mock:
        resource = boto3.resource(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="x",  # noqa: S106 - Dummy values for moto.
        )
        ensure_table(resource, "t")
        sporty = Sporty()
        respx.get(EVENTS).mock(side_effect=sporty.list_events)
        respx.post(SHARE).mock(side_effect=sporty.create)
        respx.get(url__regex=rf"{BASE}/orders/share/.+").mock(side_effect=sporty.lookup)
        yield Harness(tmp_path, resource.Table("t"), sporty)


# The normal path.


def test_a_dry_run_builds_everything_and_never_posts(harness: Harness) -> None:
    summary = harness.run()
    assert summary.status == "succeeded", summary.error
    assert summary.tickets
    assert harness.sporty.booked == []
    assert {item["status"] for item in summary.tickets} == {"dry_run"}
    assert all(item["code"].startswith("DRYRUN-") for item in summary.tickets)
    assert {item["family"] for item in summary.tickets} == {
        "handicap", "total_ft", "total_1h", "team_total",
    }  # fmt: skip
    assert [step["name"] for step in summary.steps] == [
        "RefreshIngest", "FetchOffers", "MatchTeams", "Predict", "BuildTickets",
        "RecheckOffers", "Book", "Finalize",
    ]  # fmt: skip


def test_a_run_is_traceable_and_keeps_its_artifact(harness: Harness) -> None:
    summary = harness.run()
    run = harness.query(f"RUN#{summary.run_id}")
    assert next(item for item in run if item["sk"] == "META")["status"] == "succeeded"
    ticket = harness.ticket_items()[0]
    assert ticket["run_id"] == summary.run_id
    leg = harness.legs_of(ticket["ticket_id"])[0]
    assert leg["model_version"]
    assert leg["feature_hash"]
    assert leg["offer_fetched_at"]
    assert harness.query(f"GAME#NCAA#{leg['game_id']}")
    document = json.loads(
        (harness.tmp_path / "runs" / summary.run_id / "result.json").read_text("utf-8")
    )
    assert document["final_build"]["tickets"]
    assert document["status"] == "succeeded"


def test_a_rerun_makes_new_versions_and_supersedes_the_old(harness: Harness) -> None:
    first = harness.run()
    harness.clock += timedelta(minutes=10)
    second = harness.run()
    items = harness.ticket_items()
    by_run = {
        run: [i for i in items if i["run_id"] == run] for run in (first.run_id, second.run_id)
    }
    assert {item["status"] for item in by_run[first.run_id]} == {"superseded"}
    assert {item["status"] for item in by_run[second.run_id]} == {"dry_run"}
    assert {int(item["version"]) for item in by_run[first.run_id]} == {1}
    assert {int(item["version"]) for item in by_run[second.run_id]} == {2}
    assert all(item["gsi1sk"].endswith("#v0002") for item in by_run[second.run_id])  # C20.


def test_a_rerun_with_fewer_tickets_still_supersedes_them_all(harness: Harness) -> None:
    harness.run()
    harness.clock += timedelta(minutes=10)
    harness.run(predict=lambda *_: [])
    assert {item["status"] for item in harness.ticket_items()} == {"superseded"}


def test_only_one_run_at_a_time_per_slate(harness: Harness) -> None:
    assert harness.store.acquire_lock("NCAA", SLATE, "other", NOW)
    with pytest.raises(SlateLocked):
        harness.run()
    assert harness.sporty.booked == []


def test_an_expired_lock_gives_way(harness: Harness) -> None:
    assert harness.store.acquire_lock("NCAA", SLATE, "crashed", NOW - timedelta(hours=1))
    assert harness.run().status == "succeeded"


def test_finishing_releases_the_lock(harness: Harness) -> None:
    harness.run()
    assert harness.store.acquire_lock("NCAA", SLATE, "next", NOW)


# Matching.


def test_an_unmatched_event_goes_to_the_queue_and_is_excluded(harness: Harness) -> None:
    strange = harness.events[0]
    strange["homeTeamName"] = "Mystery Team"
    summary = harness.run()
    assert summary.counts["unmatched_events"] == 1
    item = harness.query("UNMATCHED#NCAA")[0]
    assert item["sk"] == strange["eventId"]
    assert item["reason"] == "team_not_resolved"
    assert item["home_name"] == "Mystery Team"
    assert summary.counts[Reason.UNMATCHED.value] >= 1
    tickets = harness.ticket_items()
    for ticket in tickets:
        assert all(
            leg["event_id"] != strange["eventId"] for leg in harness.legs_of(ticket["ticket_id"])
        )


def test_many_unmatched_events_raise_a_warning(harness: Harness) -> None:
    for number, event in enumerate(harness.events):
        event["homeTeamName"] = f"Mystery Team {number}"
    extra = copy.deepcopy(harness.events[0])
    extra["eventId"] = "sr:match:1"
    harness.events.append(extra)
    other = copy.deepcopy(harness.events[0])
    other["eventId"] = "sr:match:2"
    harness.events.append(other)
    summary = harness.run()
    assert summary.counts["unmatched_events"] == 7
    assert any(alert["severity"] == "WARNING" for alert in harness.alerts())


def test_a_swapped_neutral_game_is_scored_from_the_canonical_side(harness: Harness) -> None:
    event = harness.events[0]
    harness.table.put_item(
        Item=_game_item(
            event,
            home_id=_team(event["awayTeamName"]),
            away_id=_team(event["homeTeamName"]),
            neutral=True,
        )
    )
    harness.run()
    handicap = [t for t in harness.ticket_items() if t["family"] == "handicap"][0]
    leg = next(
        (
            leg
            for leg in harness.legs_of(handicap["ticket_id"])
            if leg["event_id"] == event["eventId"]
        ),
        None,
    )
    assert leg is not None
    # The model has the canonical home team, SportyBet's away team, far ahead. So the pick is
    # SportyBet's "away" outcome (1715), and the specifier is still SportyBet's own.
    assert leg["outcome_id"] == "1715"
    assert leg["side"] == "home"
    assert float(leg["line"]) == -float(leg["specifier"].split("=")[1])


def test_a_game_on_a_neighbouring_slate_stays_out_without_being_queued(harness: Harness) -> None:
    event = harness.events[0]
    harness.table.delete_item(Key={"pk": f"GAME#NCAA#game-{event['eventId']}", "sk": "META"})
    next_day = (SLATE + timedelta(days=1)).isoformat()
    harness.table.put_item(
        Item=_game_item(event, slate_date_et=next_day, gsi1pk=f"SLATE#NCAA#{next_day}")
    )
    summary = harness.run()
    assert summary.counts["unmatched_events"] == 0
    assert summary.counts["matched_games"] == len(harness.events) - 1


# Booking, when allowed. Only `dry_run=False` on the context gets here: see is_dry_run's tests.


def test_a_live_run_books_verifies_and_remembers(harness: Harness) -> None:
    summary = harness.run(dry_run=False)
    assert summary.status == "succeeded", summary.error
    assert {item["status"] for item in summary.tickets} == {"verified"}
    assert len(harness.sporty.booked) == len(summary.tickets)
    # D10: nothing SportyBet does stops two legs from one game, so check what was really sent.
    for selections in harness.sporty.booked:
        events = [item["eventId"] for item in selections]
        assert len(events) == len(set(events))


def test_an_identical_rerun_reuses_verified_codes(harness: Harness) -> None:
    first = harness.run(dry_run=False)
    booked = len(harness.sporty.booked)
    harness.clock += timedelta(minutes=1)
    second = harness.run()
    assert len(harness.sporty.booked) == booked
    assert {item["code"] for item in second.tickets} == {item["code"] for item in first.tickets}


def _reject() -> dict[str, Any]:
    return {"bizCode": 19000, "message": "Selection unavailable"}


def test_a_rejection_refetches_and_retries_exactly_once(harness: Harness) -> None:
    harness.sporty.create_responses = [_reject()]
    summary = harness.run(dry_run=False)
    assert summary.status == "succeeded", summary.error
    assert len(harness.sporty.booked) == len(summary.tickets) + 1
    assert harness.sporty.booked[0] == harness.sporty.booked[1]  # Nothing else had changed.
    events = [item["event"] for item in harness.query(f"RUN#{summary.run_id}") if "event" in item]
    assert "BOOKING_REJECTED" in events


def test_a_second_rejection_fails_the_ticket_and_alerts(harness: Harness) -> None:
    harness.sporty.create_responses = [_reject(), _reject()]
    summary = harness.run(dry_run=False)
    assert summary.status == "failed"
    failed = [item for item in summary.tickets if item["status"] == "failed"]
    assert len(failed) == 1
    assert "Booking failed" in failed[0]["reason"]
    assert any(alert["severity"] == "CRITICAL" for alert in harness.alerts())
    # The others carried on: exactly two tries for it, and one for each other ticket.
    assert len(harness.sporty.booked) == len(summary.tickets) + 1


def test_the_retry_drops_a_leg_that_is_no_longer_bookable(harness: Harness) -> None:
    sporty = harness.sporty
    original = sporty.create

    def create(request: httpx.Request) -> httpx.Response:
        if not sporty.booked:
            # The rejection is because this leg just locked. Lock it in the fresh offers too.
            first = json.loads(request.content)["selections"][0]
            for event in sporty.events:
                if event["eventId"] == first["eventId"]:
                    for market in event["markets"]:
                        if (market["id"], market.get("specifier")) == (
                            first["marketId"],
                            first["specifier"],
                        ):
                            market["status"] = 2
            sporty.create_responses = [_reject()]
        return original(request)

    respx.post(SHARE).mock(side_effect=create)
    summary = harness.run(dry_run=False)
    first, second = sporty.booked[0], sporty.booked[1]
    assert second == first[1:]
    events = [item["event"] for item in harness.query(f"RUN#{summary.run_id}") if "event" in item]
    assert "REJECTED_AT_BOOKING" in events
    # The dropped leg is gone from the stored ticket, so Phase 7 can't grade it.
    stored = {
        (leg["event_id"], leg["market_id"], leg["specifier"])
        for ticket in harness.ticket_items()
        for leg in harness.legs_of(ticket["ticket_id"])
        if ticket["ticket_no"] == 1 and ticket["family"] == "handicap"
    }
    dropped = (first[0]["eventId"], first[0]["marketId"], first[0]["specifier"])
    assert dropped not in stored


def test_too_few_legs_left_after_a_rejection_fails_without_a_second_post(harness: Harness) -> None:
    for event in harness.events[2:]:
        harness.table.delete_item(Key={"pk": f"GAME#NCAA#game-{event['eventId']}", "sk": "META"})
    del harness.events[2:]
    sporty = harness.sporty
    original = sporty.create

    def create(request: httpx.Request) -> httpx.Response:
        if not sporty.booked:
            for event in sporty.events:
                for market in event["markets"]:
                    market["status"] = 2
            sporty.create_responses = [_reject()]
        return original(request)

    respx.post(SHARE).mock(side_effect=create)
    summary = harness.run(dry_run=False)
    assert summary.tickets[0]["status"] == "failed"
    assert "legs remain" in summary.tickets[0]["reason"]
    # One POST per ticket: the rejected one wasn't sent again.
    assert len(sporty.booked) == len(summary.tickets)


def test_a_timed_out_booking_is_not_retried(harness: Harness) -> None:
    harness.sporty.create_responses = [httpx.TimeoutException("slow")]
    summary = harness.run(dry_run=False)
    assert summary.tickets[0]["status"] == "failed"
    assert "outcome unknown" in summary.tickets[0]["reason"]
    assert len(harness.sporty.booked) == len(summary.tickets)


def test_a_verification_mismatch_marks_the_ticket_invalid_and_alerts(harness: Harness) -> None:
    wrong = {"bizCode": 10000, "data": {"outcomes": []}}
    harness.sporty.lookup_response = httpx.Response(200, json=wrong)
    summary = harness.run(dry_run=False)
    assert summary.status == "failed"
    assert {item["status"] for item in summary.tickets} == {"invalid"}
    assert all(item["code"] for item in summary.tickets)
    ticket = harness.ticket_items()[0]
    assert ticket["verification"]["missing"]
    assert any(alert["severity"] == "CRITICAL" for alert in harness.alerts())


def test_an_invalid_code_is_never_reused(harness: Harness) -> None:
    harness.sporty.lookup_response = httpx.Response(
        200, json={"bizCode": 10000, "data": {"outcomes": []}}
    )
    harness.run(dry_run=False)
    booked = len(harness.sporty.booked)
    harness.sporty.lookup_response = None
    harness.clock += timedelta(minutes=1)
    harness.run()
    assert len(harness.sporty.booked) > booked


def test_a_failed_lookup_marks_the_ticket_invalid(harness: Harness) -> None:
    harness.sporty.lookup_response = httpx.Response(200, json={"bizCode": 19000, "message": "no"})
    summary = harness.run(dry_run=False)
    assert {item["status"] for item in summary.tickets} == {"invalid"}
    assert "Lookup failed" in summary.tickets[0]["reason"]


def test_a_slow_run_never_books_on_stale_odds(harness: Harness) -> None:
    original = harness.store.next_version

    def slow(*args: Any) -> int:
        harness.clock += timedelta(minutes=6)
        return original(*args)

    harness.store.next_version = slow  # type: ignore[method-assign,assignment]
    summary = harness.run(dry_run=False)
    assert harness.sporty.booked == []
    assert {item["status"] for item in summary.tickets} == {"failed"}
    assert "STALE_ODDS" in summary.tickets[0]["reason"]


# Whole-run failures (invariant 7).


def test_bot_protection_fails_the_run_keeps_artifacts_and_frees_the_lock(harness: Harness) -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(403, text="denied"))
    summary = harness.run()
    assert summary.status == "failed"
    assert "BlockedError" in (summary.error or "")
    assert any(alert["severity"] == "CRITICAL" for alert in harness.alerts())
    assert (harness.tmp_path / "runs" / summary.run_id / "result.json").exists()
    assert harness.store.acquire_lock("NCAA", SLATE, "next", NOW)
    meta = next(item for item in harness.query(f"RUN#{summary.run_id}") if item["sk"] == "META")
    assert meta["status"] == "failed"


def test_bot_protection_during_booking_stops_the_run(harness: Harness) -> None:
    respx.post(SHARE).mock(return_value=httpx.Response(403, text="denied"))
    summary = harness.run(dry_run=False)
    assert summary.status == "failed"
    assert "BlockedError" in (summary.error or "")


def test_a_model_failure_fails_the_run_loudly(harness: Harness) -> None:
    def broken(*_: Any) -> list[Prediction]:
        raise ValueError("Model built for features v1, now v2")

    summary = harness.run(predict=broken)
    assert summary.status == "failed"
    assert "Model built for features" in (summary.error or "")
    assert harness.ticket_items() == []


def test_a_refresh_with_failures_is_logged_but_does_not_stop_the_run(harness: Harness) -> None:
    summary = harness.run(refresh=lambda _: SimpleNamespace(failures={"game": "boom"}))
    assert summary.status == "succeeded"
    events = [item["event"] for item in harness.query(f"RUN#{summary.run_id}") if "event" in item]
    assert "FAILURES" in events


def test_a_bad_event_is_recorded_and_the_run_carries_on(harness: Harness) -> None:
    harness.events[0]["markets"][0]["outcomes"][0]["id"] = "9999"
    summary = harness.run()
    assert summary.status == "succeeded"
    assert any(alert["severity"] == "WARNING" for alert in harness.alerts())


def test_odds_moving_during_the_recheck_are_rescored_and_logged(harness: Harness) -> None:
    sporty = harness.sporty
    original = sporty.list_events
    calls = {"count": 0}

    def moving(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 2:
            for event in sporty.events:
                for market in event["markets"]:
                    for outcome in market["outcomes"]:
                        if outcome.get("odds"):
                            outcome["odds"] = f"{float(outcome['odds']) + 0.01:.2f}"
        return original(request)

    respx.get(EVENTS).mock(side_effect=moving)
    summary = harness.run()
    events = [item["event"] for item in harness.query(f"RUN#{summary.run_id}") if "event" in item]
    assert ODDS_MOVED in events
    assert calls["count"] == 2


# Re-check helpers.


def _build(**changes: Any) -> BuildResult:
    leg = _leg_of(**changes)
    ticket = _ticket_of([leg])
    return BuildResult(tickets=(ticket,), rejections=(), assumptions=())


def _leg_of(**changes: Any) -> Any:
    from hoopsedge.engine import Leg

    fields = {
        "rank": 1, "league": "NCAA", "game_id": "g1", "event_id": "e1", "family": "total_ft",
        "market_id": "225", "specifier": "total=200.5", "line": 200.5, "team": None, "side": "over",
        "outcome_id": "12", "odds": 1.9, "p_raw": 0.6, "p_calibrated": 0.6, "p_win": 0.6,
        "p_push": 0.0, "p_lose": 0.4, "fair": 0.5, "edge": 0.1, "ev": 0.14, "tip_time": NOW,
        "model_version": "v", "feature_hash": "h", "offer_fetched_at": NOW,
    }  # fmt: skip
    return Leg.model_validate({**fields, **changes})


def _ticket_of(legs: list[Any]) -> Any:
    from hoopsedge.engine import Ticket

    return Ticket.model_validate(
        {
            "league": "NCAA", "family": "total_ft", "ticket_no": 1, "legs": tuple(legs),
            "combined_odds": 2.0, "combined_prob": 0.5, "expected_value": 0.0,
            "selection_hash": "x", "passed_gate": True, "edge_label": "measured",
        }
    )  # fmt: skip


def test_changes_between_builds_are_named() -> None:
    empty = BuildResult(tickets=(), rejections=(), assumptions=())
    assert compare_builds(_build(), _build()) == []
    assert [c.kind for c in compare_builds(_build(), empty)] == [LEG_DROPPED]
    assert [c.kind for c in compare_builds(empty, _build())] == [LEG_ADDED]
    assert [c.kind for c in compare_builds(_build(), _build(odds=2.0))] == [ODDS_MOVED]
    moved = _build(specifier="total=201.5", line=201.5)
    assert [c.kind for c in compare_builds(_build(), moved)] == [LINE_CHANGED]


def _fresh(odds: tuple[float, float] = (1.9, 1.9), **changes: Any) -> Any:
    from tests.engine_data import offer

    fields: dict[str, Any] = {
        "event_id": "e1",
        "tip_time": NOW + timedelta(hours=2),
        "fetched_at": NOW,
    }
    return offer("g1", "total_ft", 200.5, odds, **{**fields, **changes})


def _kept(item: Any = None, min_edge: float = 0.0) -> tuple[list[Any], list[Any]]:
    ticket = _ticket_of([_leg_of(outcome_id="over-id")])
    offers = {} if item is None else {("e1", "225", "total=200.5"): item}
    return still_bookable(ticket, offers, min_edge, NOW)


def test_the_retry_keeps_only_legs_that_still_qualify() -> None:
    assert len(_kept(_fresh())[0]) == 1
    assert len(_kept()[1]) == 1  # Gone from the list.
    assert len(_kept(_fresh(bookable=False))[1]) == 1
    assert len(_kept(_fresh(status="live"))[1]) == 1
    assert len(_kept(_fresh(tip_time=NOW - timedelta(minutes=1)))[1]) == 1  # Invariant 2.
    # Invariant 3: at 1.40/3.00 the fair chance is 68%, more than the model's 60%.
    assert len(_kept(_fresh((1.4, 3.0)), min_edge=0.0)[1]) == 1


def test_repricing_updates_prices_and_ticket_totals() -> None:
    kept, _ = _kept(_fresh((2.0, 1.8)))
    leg = kept[0]
    assert leg.odds == 2.0
    assert leg.fair == pytest.approx((1 / 2.0) / (1 / 2.0 + 1 / 1.8))
    assert leg.edge == pytest.approx(0.6 - leg.fair)
    assert leg.ev == pytest.approx(0.6 * 1.0 - 0.4)
    ticket = rebuild_ticket(
        _ticket_of([_leg_of()]), [leg, leg.model_copy(update={"event_id": "e2"})]
    )
    assert ticket.combined_odds == pytest.approx(4.0)
    assert ticket.combined_prob == pytest.approx(0.36)
    assert ticket.expected_value == pytest.approx(0.44)
    assert ticket.selection_hash != "x"


# Live prediction helpers.


def _history(snapshot: pl.DataFrame | None, minutes: dict[str, pl.DataFrame]) -> Any:
    history = MagicMock()
    history.latest_injuries.return_value = snapshot
    history.player_minutes.side_effect = lambda _, team, __: minutes.get(team, pl.DataFrame())
    return history


GAME = GameRef("NBA", 2027, "g", NOW + timedelta(hours=3), "h", "a", False, False, SLATE)
STARTERS = pl.DataFrame(
    {"player_id": ["star", "bench"], "minutes": [300, 100], "start_rate": [0.9, 0.1]}
)


def _snapshot(*rows: tuple[str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "player_id": [row[0] for row in rows],
            "team_id": [row[1] for row in rows],
            "status": [row[2] for row in rows],
            "captured_at": [NOW] * len(rows),
        },
        schema_overrides={"captured_at": pl.Datetime("us", "UTC")},
    )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([("star", "h", "Day-To-Day")], True),  # The owner's decision (D71).
        ([("star", "a", "Questionable")], True),
        ([("star", "h", "Doubtful")], True),
        ([("star", "h", "Unknown new status")], True),  # D29: no guess.
        ([("star", "h", "Out")], False),  # Known, so not uncertain.
        ([("bench", "h", "Day-To-Day")], False),  # Not a starter.
        ([("star", "other", "Day-To-Day")], False),  # Another team's player.
    ],
)
def test_who_counts_as_a_questionable_starter(rows: list[Any], expected: bool) -> None:
    history = _history(_snapshot(*rows), {"h": STARTERS, "a": STARTERS})
    status = injury_status(history, GAME, NOW)
    assert isinstance(status, InjuryStatus)
    assert status.questionable_starter is expected
    assert status.snapshot_at == NOW


def test_no_recent_snapshot_means_unknown() -> None:
    assert injury_status(_history(None, {}), GAME, NOW) is None


def test_a_team_with_no_minutes_yet_has_no_known_starters() -> None:
    status = injury_status(_history(_snapshot(("star", "h", "Day-To-Day")), {}), GAME, NOW)
    assert status is not None
    assert status.questionable_starter is False


@pytest.mark.parametrize("missing", ["neutral", "postseason", "season", "tip_utc"])
def test_a_game_item_missing_something_gets_no_prediction(missing: str) -> None:
    item = _game_item(_events()[0])
    assert game_ref("NCAA", item) is not None
    item.pop(missing)
    assert game_ref("NCAA", item) is None


def test_live_replays_the_same_seasons_as_training() -> None:
    # A shorter span changes every rating, because each season's priors come from the one before.
    assert history_seasons("NCAA", 2027) == [2024, 2025, 2026, 2027]
    assert history_seasons("NBA", 2027) == list(range(2019, 2028))


def test_a_d1_team_with_no_final_yet_is_not_rated_as_the_non_d1_pool(tmp_path: Path) -> None:
    folder = tmp_path / "curated/team_seasons"
    folder.mkdir(parents=True)
    pl.DataFrame(
        {"league": ["NCAA"], "season": [2027], "team_id": ["1"], "is_d1": [True]}
    ).write_parquet(folder / "team_seasons.parquet")
    empty = pl.DataFrame(schema={"tip_utc": pl.Datetime("us", "UTC"), "game_id": pl.String})

    def history() -> History:
        d1 = d1_teams(tmp_path, [2027])
        return History("NCAA", empty, pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), d1)

    assert history().pooled_team(2027, "2") == NON_D1
    # ESPN's list for the season names team 2, which hasn't played a final yet.
    listed = {"items": [{"$ref": f"https://x/teams/{team}?lang=en"} for team in ("1", "2")]}
    write_gz(tmp_path / "raw/espn/registry/NCAA-d1-2027.json.gz", listed)
    assert history().pooled_team(2027, "2") == "2"
    assert history().pooled_team(2027, "1") == "1"
    assert history().pooled_team(2027, "3") == NON_D1


def test_the_newest_model_version_is_used_and_none_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        latest_version(tmp_path, "NBA")
    (tmp_path / "NBA/20261106-1056-a").mkdir(parents=True)
    (tmp_path / "NBA/20261106-1815-b").mkdir()
    assert latest_version(tmp_path, "NBA").name == "20261106-1815-b"


def test_the_nba_run_moved_for_the_injury_rule() -> None:
    assert (LIVE_RUN_TIMES["NBA"].hour, LIVE_RUN_TIMES["NCAA"].hour) == (22, 15)  # D71.


# The one live test booking.


def _run_document(harness: Harness, tmp: Path) -> dict[str, Any]:
    summary = harness.run()
    document: dict[str, Any] = json.loads(
        (harness.tmp_path / "runs" / summary.run_id / "result.json").read_text("utf-8")
    )
    return document


def _ledger(tmp_path: Path, codes: int) -> Path:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            [{"name": f"c{i}", "code": f"C{i}", "outcome": "created"} for i in range(codes)]
        ),
        "utf-8",
    )
    return path


@pytest.fixture
def live_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")


def test_plan_chooses_two_legs_from_different_games(harness: Harness, tmp_path: Path) -> None:
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    assert len(ticket.legs) == 2
    assert len({leg.game_id for leg in ticket.legs}) == 2


def test_live_booking_makes_one_post_and_logs_it(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    ledger = _ledger(tmp_path, 4)
    attempt = live_test.book(
        harness.ctx.client, ticket, harness.ctx.config, ticket.selection_hash[:12], ledger, NOW
    )
    assert attempt["verified"] is True
    assert len(harness.sporty.booked) == 1
    assert len(harness.sporty.booked[0]) == 2
    stored = json.loads(ledger.read_text("utf-8"))
    assert len(stored) == 5
    assert stored[-1]["code"] == attempt["code"]
    assert stored[-1]["outcome"] == "created"


def test_live_booking_refuses_without_the_override(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    with pytest.raises(SystemExit, match="DRY_RUN"):
        live_test.book(
            harness.ctx.client, ticket, harness.ctx.config, ticket.selection_hash[:12],
            _ledger(tmp_path, 4), NOW,
        )  # fmt: skip
    assert harness.sporty.booked == []


def test_live_booking_refuses_a_wrong_hash(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    with pytest.raises(SystemExit, match="hash"):
        live_test.book(
            harness.ctx.client, ticket, harness.ctx.config, "wrong", _ledger(tmp_path, 4), NOW
        )
    assert harness.sporty.booked == []


def test_live_booking_refuses_past_the_cap_of_five(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    from hoopsedge.sportybet.discovery import CodeCapError

    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    with pytest.raises(CodeCapError):
        live_test.book(
            harness.ctx.client, ticket, harness.ctx.config, ticket.selection_hash[:12],
            _ledger(tmp_path, 5), NOW,
        )  # fmt: skip
    assert harness.sporty.booked == []


def test_live_booking_refuses_more_than_two_legs(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    bigger = ticket.model_copy(update={"legs": (*ticket.legs, ticket.legs[0])})
    with pytest.raises(SystemExit, match="exactly two"):
        live_test.book(
            harness.ctx.client, bigger, harness.ctx.config, "x", _ledger(tmp_path, 4), NOW
        )
    assert harness.sporty.booked == []


def test_live_booking_refuses_a_leg_that_has_since_locked(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    for event in harness.sporty.events:
        for market in event["markets"]:
            market["status"] = 2
    ledger = _ledger(tmp_path, 4)
    with pytest.raises(SystemExit, match="no longer qualify"):
        live_test.book(
            harness.ctx.client, ticket, harness.ctx.config, ticket.selection_hash[:12], ledger, NOW
        )
    assert harness.sporty.booked == []
    assert len(json.loads(ledger.read_text("utf-8"))) == 4


def test_the_ledger_records_a_booking_before_the_post(
    harness: Harness, tmp_path: Path, live_ready: None
) -> None:
    # A crash after the POST must not free a slot, so a timeout leaves the entry pending.
    ticket = live_test.choose_ticket(_run_document(harness, tmp_path))
    ledger = _ledger(tmp_path, 4)
    harness.sporty.create_responses = [httpx.TimeoutException("slow")]
    with pytest.raises(Exception, match="outcome unknown"):
        live_test.book(
            harness.ctx.client, ticket, harness.ctx.config, ticket.selection_hash[:12], ledger, NOW
        )
    stored = json.loads(ledger.read_text("utf-8"))
    assert stored[-1]["outcome"] == "pending"
    from hoopsedge.sportybet.discovery import CodeCapError, assert_code_available

    with pytest.raises(CodeCapError):
        assert_code_available(stored)


def test_the_plan_refuses_a_run_that_cannot_supply_two_legs(
    harness: Harness, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit, match="no tickets"):
        live_test.choose_ticket({"final_build": {"tickets": []}})
    document = _run_document(harness, tmp_path)
    document["final_build"]["tickets"][0]["legs"] = document["final_build"]["tickets"][0]["legs"][
        :1
    ]
    with pytest.raises(SystemExit, match="fewer than two"):
        live_test.choose_ticket(document)


# Store and game details.


def test_a_game_item_that_cannot_be_read_is_left_out_not_guessed(harness: Harness) -> None:
    event = harness.events[0]
    broken = _game_item(event, tip_utc="not a time")
    harness.table.put_item(Item=broken)
    summary = harness.run()
    assert summary.status == "succeeded"
    assert summary.counts["unmatched_events"] == 1


def test_owner_confirmed_aliases_are_read_back(harness: Harness) -> None:
    harness.table.put_item(Item={"pk": "ALIAS#NCAA", "sk": "mystery team", "team_id": "t9"})
    assert harness.store.confirmed_aliases("NCAA") == [
        {"alias_normalized": "mystery team", "team_id": "t9"}
    ]


def test_a_database_failure_on_the_lock_is_not_mistaken_for_a_held_lock(harness: Harness) -> None:
    error = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem")
    harness.store.table = MagicMock()
    harness.store.table.put_item.side_effect = error
    with pytest.raises(ClientError):
        harness.store.acquire_lock("NCAA", SLATE, "run", NOW)
    harness.store.table.delete_item.side_effect = error
    with pytest.raises(ClientError):
        harness.store.release_lock("NCAA", SLATE, "run")


def test_another_runs_lock_is_not_released(harness: Harness) -> None:
    assert harness.store.acquire_lock("NCAA", SLATE, "mine", NOW)
    harness.store.release_lock("NCAA", SLATE, "someone else")
    assert not harness.store.acquire_lock("NCAA", SLATE, "third", NOW)


# Mapping model output to predictions, with the model itself stubbed.


def _ref(game_id: str, league: str = "NBA") -> GameRef:
    return GameRef(league, 2027, game_id, NOW + timedelta(hours=3), "h", "a", False, False, SLATE)


def _feature_row(game_id: str, as_of: datetime) -> FeatureRow:
    values: dict[str, float | None] = dict.fromkeys(FEATURES, 1.0)
    values.update(games_h=7.0, games_a=9.0, prior_weight_h=0.3, prior_weight_a=0.2)
    return FeatureRow(game_id, as_of, FEATURE_SET_VERSION, f"hash-{game_id}", values)


def _distribution(base: float) -> ScoreDistribution:
    def array(offset: float) -> np.ndarray:
        return np.array([base + offset, base + offset + 100.0])

    return ScoreDistribution(array(0), array(1), array(2), array(3), array(4), array(5))


def _stub_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[datetime]:
    (tmp_path / "feature_set_version.txt").write_text(FEATURE_SET_VERSION, "utf-8")
    seen: list[datetime] = []

    def features(_: Any, game: GameRef, as_of: datetime) -> FeatureRow:
        seen.append(as_of)
        return _feature_row(game.game_id, as_of)

    bundle = MagicMock()
    bundle.predict.side_effect = lambda _, name: _distribution(10.0 if name == "ft" else 50.0)
    monkeypatch.setattr(predict_module, "History", MagicMock())
    monkeypatch.setattr(predict_module, "Engine", MagicMock())
    monkeypatch.setattr(predict_module, "features_for_game", features)
    monkeypatch.setattr(predict_module, "load_bundle", lambda _: bundle)
    monkeypatch.setattr(predict_module, "injury_status", lambda *_: None)
    return seen


def test_each_game_gets_its_own_row_of_each_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_model(monkeypatch, tmp_path)
    first, second = predict_games(tmp_path, "NBA", [_ref("a"), _ref("b")], NOW, tmp_path)
    assert first.game_id == "a"
    assert first.model_version == tmp_path.name
    assert first.feature_hash == "hash-a"
    assert first.ft is not None and second.ft is not None and second.h1 is not None
    assert (first.ft.mu_home, first.ft.mu_away, first.ft.sd_margin) == (10.0, 11.0, 12.0)
    assert (first.ft.sd_total, first.ft.sd_home, first.ft.sd_away) == (13.0, 14.0, 15.0)
    assert second.ft.mu_home == 110.0
    assert second.h1.mu_home == 150.0
    assert first.confidence.games_home == 7
    assert first.confidence.prior_weight_away == 0.2


def test_a_game_that_already_tipped_is_featured_at_its_tip_not_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _stub_model(monkeypatch, tmp_path)
    late = NOW + timedelta(hours=5)
    predict_games(tmp_path, "NBA", [_ref("a"), _ref("b")], late, tmp_path)
    assert seen == [NOW + timedelta(hours=3)] * 2


def test_injuries_are_only_for_the_nba(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_model(monkeypatch, tmp_path)
    called: list[str] = []
    monkeypatch.setattr(
        predict_module, "injury_status", lambda _, game, __: called.append(game.game_id)
    )
    predict_games(tmp_path, "NCAA", [_ref("a", "NCAA")], NOW, tmp_path)
    assert called == []
    predict_games(tmp_path, "NBA", [_ref("a"), _ref("b")], NOW, tmp_path)
    assert called == ["a", "b"]


def test_a_model_built_for_other_features_is_refused(tmp_path: Path) -> None:
    (tmp_path / "feature_set_version.txt").write_text("old-version", "utf-8")
    with pytest.raises(ValueError, match="old-version"):
        predict_games(tmp_path, "NBA", [_ref("a")], NOW, tmp_path)


def test_no_games_needs_no_model(tmp_path: Path) -> None:
    assert predict_games(tmp_path, "NBA", [], NOW, tmp_path) == []


def test_a_recorded_event_parses_the_way_the_pipeline_needs() -> None:
    assert parse_event("NCAA", _events()[0]).status == "not_started"
