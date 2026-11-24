"""The SportyBet client (D11, D30, D81): reads, blocks, business errors, one POST per code.

The v1 offer-parsing tests went to `legacy/tests` with `sportybet/offers.py` (D96). Event lists
are exercised through v2's `Runner.events`, which keeps v1's guards on overlapping pages.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hoopsedge.common.config_v2 import load_defaults
from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.sportybet.client import (
    ENDPOINTS,
    BlockedError,
    BusinessError,
    SportyBetClient,
    SportyBetError,
    build_client,
)
from hoopsedge.v2.slate import RunFailed, Runner

FIXTURES = Path(__file__).parent / "fixtures/sportybet"
BASE = ENDPOINTS["base_url"]
EVENTS = f"{BASE}{ENDPOINTS['events']['path']}"
SHARE = f"{BASE}{ENDPOINTS['share_create']['path']}"
NCAAB = load_defaults().sports["NCAAB"]
NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def _client(tmp_path: Path | None = None) -> SportyBetClient:
    http = PoliteClient("test", rate_per_second=1000, max_retries=1, sleep=lambda _: None)
    return SportyBetClient(http, tmp_path)


def _runner(tmp_path: Path | None = None) -> Runner:
    return Runner(_client(tmp_path), tmp_path or Path(), clock=lambda: NOW)


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
    events = _runner(tmp_path).events(NCAAB)
    assert route.call_count == 2
    assert len(events) == total == 8
    assert len({event["eventId"] for event in events}) == total
    assert list(tmp_path.glob("events-*.json.gz")), "Every raw response is kept"
    sent = route.calls[0].request.url.params
    assert (sent["sportId"], sent["tournamentId"]) == ("sr:sport:2", "sr:tournament:648")


@respx.mock
def test_fewer_events_than_the_total_says_is_a_loud_failure() -> None:
    events = _events("events-ncaa-page1.json")
    respx.get(EVENTS).mock(
        side_effect=[httpx.Response(200, json=_page(events, 44)),
                     httpx.Response(200, json=_page(events, 44))]
    )  # fmt: skip
    with pytest.raises(RunFailed, match="of 44"):
        _runner().events(NCAAB)


@respx.mock
def test_a_repeating_api_cannot_hold_the_loop_forever() -> None:
    events = _events("events-nba-page1.json")
    route = respx.get(EVENTS).mock(return_value=httpx.Response(200, json=_page(events, 99)))
    with pytest.raises(RunFailed):
        _runner().events(NCAAB)
    assert route.call_count == 2


@respx.mock
def test_an_empty_list_is_valid() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=_page([], 0)))
    assert _runner().events(NCAAB) == []


@respx.mock
def test_failure_with_http_200_is_never_treated_as_data() -> None:
    respx.get(EVENTS).mock(
        return_value=httpx.Response(200, json={"bizCode": 19000, "message": "no"})
    )
    with pytest.raises(BusinessError) as caught:
        _runner().events(NCAAB)
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
        _runner().events(NCAAB)


@respx.mock
def test_a_server_error_after_retries_is_loud() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(503))
    with pytest.raises(SportyBetError):
        _runner().events(NCAAB)


@respx.mock
def test_a_response_of_the_wrong_shape_is_loud() -> None:
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(SportyBetError, match="shape"):
        _runner().events(NCAAB)


@respx.mock
def test_event_detail_asks_for_the_prematch_product() -> None:
    document = _load("ncaaf-event-detail.json")
    route = respx.get(f"{BASE}{ENDPOINTS['event_detail']['path']}").mock(
        return_value=httpx.Response(200, json=document)
    )
    snapshot = _runner().detail("sr:match:70919162")
    assert route.calls[0].request.url.params["productId"] == "3"
    assert snapshot.lines.event_id == "sr:match:70919162" and snapshot.fetched_at == NOW


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
