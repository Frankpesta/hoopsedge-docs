"""The client, offers and verification against recorded responses (docs/07, D30, D33, D70)."""

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.common.ids import new_ulid
from hoopsedge.engine import Leg
from hoopsedge.sportybet.booking import dry_run_code, is_dry_run, selections
from hoopsedge.sportybet.client import (
    ENDPOINTS,
    BlockedError,
    BusinessError,
    SportyBetClient,
    SportyBetError,
    build_client,
)
from hoopsedge.sportybet.offers import (
    fetch_snapshot,
    normalize_event,
    parse_event,
    slate_bounds,
)
from hoopsedge.sportybet.verification import verify_code
from hoopsedge.teams.matcher import NORMAL, SWAPPED
from tests.engine_data import NOW

FIXTURES = Path(__file__).parent / "fixtures/sportybet"
BASE = ENDPOINTS["base_url"]
EVENTS = f"{BASE}{ENDPOINTS['events']['path']}"
SHARE = f"{BASE}{ENDPOINTS['share_create']['path']}"


def _load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def _client(tmp_path: Path | None = None) -> SportyBetClient:
    http = PoliteClient("test", rate_per_second=1000, max_retries=1, sleep=lambda _: None)
    return SportyBetClient(http, tmp_path)


def _page(events: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "bizCode": ENDPOINTS["success_biz_code"],
        "data": {"totalNum": total, "tournaments": [{"events": events}]},
    }


def _events(name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = _load(name)["data"]["tournaments"][0]["events"]
    return events


# Reads.


@respx.mock
def test_real_overlapping_pages_give_each_event_once(tmp_path: Path) -> None:
    first, second = _events("events-ncaa-page1.json"), _events("events-ncaa-page2.json")
    total = len({event["eventId"] for event in [*first, *second]})
    route = respx.get(EVENTS).mock(
        side_effect=[httpx.Response(200, json=_page(first, total)),
                     httpx.Response(200, json=_page(second, total))]
    )  # fmt: skip
    snapshot = fetch_snapshot(_client(tmp_path), "NCAA", NOW)
    assert route.call_count == 2
    assert len(snapshot.events) == total == 8
    assert len({event.event_id for event in snapshot.events}) == total
    assert not snapshot.failures
    assert list(tmp_path.glob("events-*.json.gz")), "Every raw response is kept"


@respx.mock
def test_fewer_events_than_the_total_says_is_a_loud_failure() -> None:
    events = _events("events-ncaa-page1.json")
    respx.get(EVENTS).mock(
        side_effect=[httpx.Response(200, json=_page(events, 44)),
                     httpx.Response(200, json=_page(events, 44))]
    )  # fmt: skip
    with pytest.raises(SportyBetError, match="of 44"):
        fetch_snapshot(_client(), "NCAA", NOW)


@respx.mock
def test_a_repeating_api_cannot_hold_the_loop_forever() -> None:
    events = _events("events-nba-page1.json")
    route = respx.get(EVENTS).mock(return_value=httpx.Response(200, json=_page(events, 99)))
    with pytest.raises(SportyBetError):
        fetch_snapshot(_client(), "NBA", NOW)
    assert route.call_count == 2


@respx.mock
def test_an_empty_list_is_a_valid_empty_snapshot() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=_page([], 0)))
    assert fetch_snapshot(_client(), "NBA", NOW).events == ()


@respx.mock
def test_failure_with_http_200_is_never_treated_as_data() -> None:
    respx.get(EVENTS).mock(
        return_value=httpx.Response(200, json={"bizCode": 19000, "message": "no"})
    )
    with pytest.raises(BusinessError) as caught:
        fetch_snapshot(_client(), "NBA", NOW)
    assert caught.value.code == 19000


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="denied"),
        httpx.Response(200, text="<html>Just a moment...</html>"),
    ],
)
@respx.mock
def test_bot_protection_stops_with_its_own_error(response: httpx.Response) -> None:
    respx.get(EVENTS).mock(return_value=response)
    with pytest.raises(BlockedError):
        fetch_snapshot(_client(), "NBA", NOW)


@respx.mock
def test_a_server_error_after_retries_is_loud() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(503))
    with pytest.raises(SportyBetError):
        fetch_snapshot(_client(), "NBA", NOW)


@respx.mock
def test_a_response_of_the_wrong_shape_is_loud() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(SportyBetError, match="shape"):
        fetch_snapshot(_client(), "NBA", NOW)


@pytest.mark.parametrize(
    "response",
    [httpx.Response(403, text="denied"), httpx.Response(200, text="<html>challenge</html>")],
)
@respx.mock
def test_a_lookup_blocked_by_bot_protection_has_its_own_error(response: httpx.Response) -> None:
    respx.get(f"{BASE}/orders/share/ABC").mock(return_value=response)
    with pytest.raises(BlockedError):
        _client().lookup("ABC")


@pytest.mark.parametrize(
    "response",
    [httpx.Response(403, text="denied"), httpx.Response(200, text="<html>challenge</html>")],
)
@respx.mock
def test_a_booking_blocked_by_bot_protection_has_its_own_error(response: httpx.Response) -> None:
    route = respx.post(SHARE).mock(return_value=response)
    with pytest.raises(BlockedError):
        _client().create_code([])
    assert route.call_count == 1


def test_the_real_client_is_polite_and_descriptive(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    assert isinstance(client, SportyBetClient)
    assert client._http._interval == 1.0  # noqa: SLF001 - One request a second (D30).


# Creating a code.


@respx.mock
def test_a_code_is_created_with_exactly_one_post() -> None:
    route = respx.post(SHARE).mock(
        return_value=httpx.Response(200, json=_load("share-create-all-markets.json"))
    )
    code = _client().create_code([{"eventId": "e", "marketId": "m", "outcomeId": "o"}])
    assert code
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"selections": [{"eventId": "e", "marketId": "m", "outcomeId": "o"}]}
    assert route.calls[0].request.headers["OperId"] == "2"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.TimeoutException("slow"), httpx.ConnectError("lost")],
)
@respx.mock
def test_a_failed_post_is_never_retried(response: Any) -> None:
    # A retry could create a second real code, so an unknown outcome stays unknown.
    route = respx.post(SHARE).mock(side_effect=[response])
    with pytest.raises(SportyBetError):
        _client().create_code([])
    assert route.call_count == 1


@respx.mock
def test_a_rejection_raises_with_the_business_code() -> None:
    respx.post(SHARE).mock(
        return_value=httpx.Response(200, json={"bizCode": 19000, "message": "x"})
    )
    with pytest.raises(BusinessError):
        _client().create_code([])


@respx.mock
def test_success_with_no_code_is_not_success() -> None:
    respx.post(SHARE).mock(return_value=httpx.Response(200, json={"bizCode": 10000, "data": {}}))
    with pytest.raises(SportyBetError, match="no code"):
        _client().create_code([])


@respx.mock
def test_post_once_reports_an_unknown_outcome() -> None:
    respx.post("https://example.test/x").mock(side_effect=httpx.ConnectTimeout("slow"))
    http = PoliteClient("test", rate_per_second=1000, sleep=lambda _: None)
    with pytest.raises(HttpFetchError, match="outcome unknown"):
        http.post_json_once("https://example.test/x", {}, {})


# Parsing offers.


def _event() -> dict[str, Any]:
    return copy.deepcopy(_events("events-ncaa-page1.json")[0])


def _market(document: dict[str, Any], market_id: str) -> dict[str, Any]:
    return next(item for item in document["markets"] if item["id"] == market_id)


def test_every_recorded_event_parses_and_normalizes() -> None:
    for name, league in (
        ("events-nba-page1.json", "NBA"),
        ("events-ncaa-page1.json", "NCAA"),
        ("events-ncaa-page2.json", "NCAA"),
    ):
        for document in _events(name):
            event = parse_event(league, document)
            offers = normalize_event(event, "g", NORMAL, NOW)
            assert offers
            assert {item.family for item in offers} <= {
                "handicap",
                "total_ft",
                "total_1h",
                "team_total",
            }


@pytest.mark.parametrize(
    ("change", "text"),
    [
        (lambda m: m.update(specifier="foo=1.5"), "specifier"),
        (lambda m: m.update(specifier="hcp=abc"), "could not convert"),
        (lambda m: m["outcomes"][0].update(id="9999"), "unknown outcomes"),
        (lambda m: m["outcomes"][0].update(odds="1.00"), "unusable odds"),
        (lambda m: m["outcomes"][0].update(odds="abc"), "unusable odds"),
        (lambda m: m.pop("status"), "status"),
    ],
)
def test_an_unexpected_market_shape_is_refused(change: Any, text: str) -> None:
    document = _event()
    market = next(
        item for item in document["markets"] if item["id"] == "223" and item["status"] == 0
    )
    change(market)
    with pytest.raises(ValueError, match=text):
        parse_event("NCAA", document)


@respx.mock
def test_a_bad_event_is_recorded_and_skipped_and_the_others_survive() -> None:
    events = _events("events-ncaa-page1.json")
    events[1]["markets"][0]["outcomes"][0]["id"] = "9999"
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=_page(events, len(events))))
    snapshot = fetch_snapshot(_client(), "NCAA", NOW)
    assert len(snapshot.events) == len(events) - 1
    assert [failure[0] for failure in snapshot.failures] == [events[1]["eventId"]]


def test_a_market_we_do_not_use_is_ignored_whatever_its_shape() -> None:
    document = _event()
    document["markets"].append({"id": "18", "weird": True})
    assert parse_event("NCAA", document).raw.markets


def test_a_locked_line_with_no_price_is_skipped_rather_than_refused() -> None:
    document = _event()
    market = next(item for item in document["markets"] if item["status"] != 0)
    market["outcomes"][0].pop("odds")
    event = parse_event("NCAA", document)
    offers = normalize_event(event, "g", NORMAL, NOW)
    assert (market["id"], market["specifier"]) not in {
        (item.market_id, item.specifier) for item in offers
    }


def test_locked_and_open_lines_are_told_apart() -> None:
    offers = normalize_event(parse_event("NCAA", _event()), "g", NORMAL, NOW)
    assert {item.bookable for item in offers} == {True, False}


def test_a_swapped_event_flips_what_it_means_but_not_what_gets_booked() -> None:
    document = _event()
    event = parse_event("NCAA", document)
    normal = normalize_event(event, "g", NORMAL, NOW)
    flipped = normalize_event(event, "g", SWAPPED, NOW)
    assert len(normal) == len(flipped)
    for before, after in zip(normal, flipped, strict=True):
        assert after.specifier == before.specifier
        assert after.market_id == before.market_id
        by_id = {item.outcome_id: item.side for item in after.outcomes}
        if before.family == "handicap":
            assert after.line == -before.line
            for item in before.outcomes:
                assert by_id[item.outcome_id] == {"home": "away", "away": "home"}[item.side]
        elif before.family == "team_total":
            assert after.team == {"home": "away", "away": "home"}[before.team or ""]
            assert after.line == before.line
        else:
            assert after.line == before.line
            assert by_id == {item.outcome_id: item.side for item in before.outcomes}


def test_the_slate_window_is_six_hours_past_the_eastern_date() -> None:
    first, last = slate_bounds(date(2026, 11, 6))
    assert first == datetime(2026, 11, 5, 23, 0, tzinfo=UTC)  # Midnight EST is 05:00 UTC, less 6 h.
    assert last == datetime(2026, 11, 7, 11, 0, tzinfo=UTC)


# Booking rules.


def _leg(
    specifier: str = "total=200.5", rank: int = 1, event: str = "e1", odds: float = 1.91
) -> Leg:
    return Leg.model_validate(
        {
            "rank": rank, "league": "NCAA", "game_id": event, "event_id": event,
            "family": "total_ft", "market_id": "225", "specifier": specifier, "line": 200.5,
            "team": None, "side": "over", "outcome_id": "12", "odds": odds, "p_raw": 0.6,
            "p_calibrated": 0.6, "p_win": 0.6, "p_push": 0.0, "p_lose": 0.4, "fair": 0.5,
            "edge": 0.1, "ev": 0.1, "tip_time": NOW, "model_version": "v", "feature_hash": "h",
            "offer_fetched_at": NOW,
        }
    )  # fmt: skip


@pytest.mark.parametrize(
    ("env", "dry", "config", "expected"),
    [
        ("local", "false", False, True),  # A local run can never create a real code.
        ("local", "true", False, True),
        ("dev", "true", False, True),
        ("dev", "", False, True),
        ("dev", "false", True, True),  # Either the environment or the config can force dry-run.
        ("dev", "false", False, False),
        ("prod", "FALSE", False, False),
    ],
)
def test_booking_needs_both_environment_and_config(
    monkeypatch: pytest.MonkeyPatch, env: str, dry: str, config: bool, expected: bool
) -> None:
    monkeypatch.setenv("HOOPSEDGE_ENV", env)
    monkeypatch.setenv("DRY_RUN", dry)
    assert is_dry_run(config) is expected


def test_selections_match_what_phase_zero_recorded_and_sort_by_rank() -> None:
    sent = selections([_leg(rank=2, event="e2"), _leg(rank=1, event="e1")])
    assert sent[0] == {
        "eventId": "e1",
        "marketId": "225",
        "specifier": "total=200.5",
        "outcomeId": "12",
    }
    assert sent[1]["eventId"] == "e2"


def test_a_dry_run_code_is_marked() -> None:
    assert dry_run_code("01ABCDEFGH") == "DRYRUN-CDEFGH"


# Verification (invariant 4).


def _lookup(*selected: tuple[str, str, str, str, str]) -> dict[str, Any]:
    return {
        "bizCode": 10000,
        "data": {
            "outcomes": [
                {
                    "eventId": event,
                    "markets": [
                        {
                            "id": market,
                            "specifier": specifier,
                            "outcomes": [{"id": outcome, "odds": odds}],
                        }
                    ],
                }
                for event, market, specifier, outcome, odds in selected
            ]
        },
    }


GOOD = ("e1", "225", "total=200.5", "12", "1.91")


def test_an_exact_match_verifies() -> None:
    result = verify_code([_leg()], _lookup(GOOD))
    assert result.verified
    assert result.reason is None


@pytest.mark.parametrize(
    ("selected", "missing", "extra"),
    [
        ((), 1, 0),
        ((("e2", "225", "total=200.5", "12", "1.91"),), 1, 1),  # Another event.
        ((("e1", "18", "total=200.5", "12", "1.91"),), 1, 1),  # Another market.
        ((("e1", "225", "total=201.5", "12", "1.91"),), 1, 1),  # Another line.
        ((("e1", "225", "total=200.5", "13", "1.91"),), 1, 1),  # The other side.
        ((GOOD, ("e9", "225", "total=200.5", "12", "1.91")), 0, 1),  # An extra leg.
    ],
)
def test_any_difference_fails_verification(
    selected: tuple[tuple[str, str, str, str, str], ...], missing: int, extra: int
) -> None:
    result = verify_code([_leg()], _lookup(*selected))
    assert not result.verified
    assert (len(result.missing), len(result.extra)) == (missing, extra)
    assert result.reason


@pytest.mark.parametrize("bad", [{}, {"data": None}, {"data": {"outcomes": [{"eventId": "e1"}]}}])
def test_an_unreadable_lookup_is_not_verified(bad: dict[str, Any]) -> None:
    result = verify_code([_leg()], bad)
    assert not result.verified
    assert "unreadable" in (result.reason or "")


def test_a_small_price_difference_is_fine_and_a_larger_one_is_recorded() -> None:
    near = verify_code([_leg()], _lookup(("e1", "225", "total=200.5", "12", "1.93")))
    assert near.verified
    assert near.odds_differences == ()
    far = verify_code([_leg()], _lookup(("e1", "225", "total=200.5", "12", "1.95")))
    assert far.verified  # The selections match; the price moved, and that's recorded.
    assert far.odds_differences == ((("e1", "225", "total=200.5", "12"), 1.91, 1.95),)


def test_the_real_recorded_lookup_verifies_against_its_own_ledger() -> None:
    document = _load("share-lookup-two-leg.json")
    event = document["data"]["outcomes"][0]
    market = event["markets"][0]
    leg = _leg(market.get("specifier", ""), event=str(event["eventId"]))
    leg = leg.model_copy(update={"market_id": str(market["id"]),
                                 "outcome_id": str(market["outcomes"][0]["id"])})  # fmt: skip
    result = verify_code([leg], document)
    assert (len(result.missing), len(result.extra)) == (0, len(document["data"]["outcomes"]) - 1)


# IDs and small helpers.


def test_ulids_are_26_characters_and_sort_by_time() -> None:
    first = new_ulid(NOW)
    later = new_ulid(NOW.replace(year=2027))
    assert len(first) == 26
    assert first < later


# D78: on this machine, only the dashboard's Run now can book, and only with every switch set.


@pytest.mark.parametrize(
    ("allowed", "dry", "trigger", "config", "expected"),
    [
        ("dashboard", "false", "dashboard", False, False),
        ("dashboard", "false", "scheduled", False, True),
        ("dashboard", "false", "manual", False, True),
        ("dashboard", "false", None, False, True),
        ("dashboard", "false", "dashboard", True, True),  # The Settings switch still wins.
        ("dashboard", "true", "dashboard", False, True),
        ("", "false", "dashboard", False, True),
        ("anything", "false", "dashboard", False, True),
    ],
)
def test_local_booking_is_only_ever_run_now(
    monkeypatch: pytest.MonkeyPatch,
    allowed: str,
    dry: str,
    trigger: str | None,
    config: bool,
    expected: bool,
) -> None:
    monkeypatch.setenv("HOOPSEDGE_ENV", "local")
    monkeypatch.setenv("DRY_RUN", dry)
    monkeypatch.setenv("HOOPSEDGE_LOCAL_BOOKING", allowed)
    assert is_dry_run(config, trigger) is expected
