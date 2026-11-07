"""D81: SportyBet, and only SportyBet, through a Nigerian residential proxy (D7).

The proxy is set by `HOOPSEDGE_SPORTYBET_PROXY` on the server. Its address holds a password, so it
must never reach a log, an error or an alert. A failed proxy fails the call: nothing falls back to
going direct, which bot protection challenges (invariant 7).
"""

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.ingest import collector
from hoopsedge.sportybet import client as sportybet
from hoopsedge.sportybet.client import BlockedError, build_client, proxy_url

SECRET = "s3cr3t-pass_country-ng"  # noqa: S105 - A made-up value to check it never leaks.
PROXY = f"http://hoops-user:{SECRET}@geo.proxy.example:12321"
EVENTS = f"{sportybet.ENDPOINTS['base_url']}{sportybet.ENDPOINTS['events']['path']}"


class Captured:
    """Records what each httpx.Client was built with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, real: Any) -> Any:
        def build(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return real(*args, **kwargs)

        return build


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    seen = Captured()
    monkeypatch.setattr(httpx, "Client", seen(httpx.Client))
    return seen


def test_no_proxy_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOPSEDGE_SPORTYBET_PROXY", raising=False)
    assert proxy_url() is None
    monkeypatch.setenv("HOOPSEDGE_SPORTYBET_PROXY", "  ")
    assert proxy_url() is None


@pytest.mark.parametrize("bad", ["geo.proxy.example:12321", "ftp://x:1", "http://"])
def test_a_malformed_proxy_stops_rather_than_going_direct(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("HOOPSEDGE_SPORTYBET_PROXY", bad)
    with pytest.raises(ValueError, match="HOOPSEDGE_SPORTYBET_PROXY") as caught:
        proxy_url()
    assert SECRET not in str(caught.value)


def test_the_booking_client_uses_the_proxy(
    monkeypatch: pytest.MonkeyPatch, captured: Captured, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOOPSEDGE_SPORTYBET_PROXY", PROXY)
    build_client(tmp_path)
    assert [call.get("proxy") for call in captured.calls] == [PROXY]


def test_only_the_collectors_sportybet_client_uses_the_proxy(
    monkeypatch: pytest.MonkeyPatch, captured: Captured, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOOPSEDGE_SPORTYBET_PROXY", PROXY)
    monkeypatch.setenv("HOOPSEDGE_DATA_DIR", str(tmp_path))
    collector._context()  # noqa: SLF001
    assert [call.get("proxy") for call in captured.calls] == [PROXY, None]  # SportyBet, ESPN.


def test_without_a_proxy_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, captured: Captured, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOOPSEDGE_SPORTYBET_PROXY", raising=False)
    build_client(tmp_path)
    assert captured.calls[0].get("proxy") is None


def test_a_failed_proxy_fails_loudly_without_the_password() -> None:
    client = PoliteClient("test/1.0", max_retries=1, sleep=lambda _: None, proxy=PROXY)
    with respx.mock:
        respx.get(EVENTS).mock(side_effect=httpx.ProxyError("407 Proxy Authentication Required"))
        with pytest.raises(HttpFetchError) as caught:
            client.get_json(EVENTS)
    message = str(caught.value)
    assert "proxy" in message.lower()
    assert SECRET not in message
    assert "hoops-user" not in message


def test_a_failed_proxy_post_fails_once_without_the_password() -> None:
    client = PoliteClient("test/1.0", proxy=PROXY)
    with respx.mock:
        route = respx.post(EVENTS).mock(side_effect=httpx.ProxyError("tunnel failed"))
        with pytest.raises(HttpFetchError) as caught:
            client.post_json_once(EVENTS, {}, {})
    assert route.call_count == 1  # Never a second POST: it could create a second code.
    assert SECRET not in str(caught.value)


@respx.mock
def test_a_waf_challenge_is_bot_protection() -> None:
    # What CloudFront sent from the EC2 server on 2026-11-07 (D81).
    respx.get(EVENTS).mock(
        return_value=httpx.Response(202, headers={"x-amzn-waf-action": "challenge"}, text="")
    )
    client = sportybet.SportyBetClient(PoliteClient("test/1.0", max_retries=0))
    with pytest.raises(BlockedError):
        client.events_page("sr:tournament:132", 1)


def test_the_collector_halts_sportybet_on_a_waf_challenge() -> None:
    assert collector._is_block(HttpFetchError("request rejected", EVENTS, 202))  # noqa: SLF001
