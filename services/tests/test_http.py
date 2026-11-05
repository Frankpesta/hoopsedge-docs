"""Tests for the shared HTTP client (docs/03-data-ingestion.md section 2.1)."""

from pathlib import Path

import httpx
import pytest
import respx

from hoopsedge.common.http import HttpFetchError, PoliteClient

URL = "https://api.example.com/data"


class FakeClock:
    """Time that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(clock: FakeClock, **kwargs: object) -> PoliteClient:
    return PoliteClient(
        user_agent="test-client/1.0",
        sleep=clock.sleep,
        clock=clock.time,
        rng=lambda: 0.5,
        **kwargs,  # type: ignore[arg-type]
    )


@respx.mock
def test_returns_json_and_sends_user_agent() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert make_client(FakeClock()).get_json(URL) == {"ok": True}
    assert route.calls[0].request.headers["user-agent"] == "test-client/1.0"


@respx.mock
def test_rate_limit_spaces_requests_per_host() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    clock = FakeClock()
    client = make_client(clock, rate_per_second=2.0)
    client.get_json(URL)
    client.get_json(URL)
    client.get_json(URL)
    assert clock.sleeps == [0.5, 0.5]


@respx.mock
def test_rate_limit_is_separate_for_each_host() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    other = "https://other.example.com/data"
    respx.get(other).mock(return_value=httpx.Response(200, json={}))
    clock = FakeClock()
    client = make_client(clock, rate_per_second=2.0)
    client.get_json(URL)
    client.get_json(other)
    assert clock.sleeps == []


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_transient_status_then_succeeds(status: int) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json={"ok": 1})]
    )
    client = make_client(FakeClock())
    assert client.get_json(URL) == {"ok": 1}
    assert route.call_count == 2
    assert client.stats.retries == 1
    assert client.stats.by_status[status] == 1


@respx.mock
def test_retries_timeouts() -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={})]
    )
    assert make_client(FakeClock()).get_json(URL) == {}
    assert route.call_count == 2


@respx.mock
def test_backoff_grows_exponentially_with_jitter() -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})]
    )
    clock = FakeClock()
    make_client(clock, rate_per_second=1000.0).get_json(URL)
    backoffs = [s for s in clock.sleeps if s > 0.01]
    assert backoffs == [0.75, 1.5]


@respx.mock
def test_honours_retry_after_header() -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )
    clock = FakeClock()
    make_client(clock, rate_per_second=1000.0).get_json(URL)
    assert max(clock.sleeps) == 7.0


@respx.mock
def test_fails_loudly_after_five_retries() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(HttpFetchError) as err:
        make_client(FakeClock()).get_json(URL)
    assert route.call_count == 6
    assert err.value.status == 503


@respx.mock
def test_does_not_retry_permanent_errors() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(HttpFetchError) as err:
        make_client(FakeClock()).get_json(URL)
    assert route.call_count == 1
    assert err.value.status == 404


@respx.mock
def test_invalid_json_fails_loudly() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(HttpFetchError):
        make_client(FakeClock()).get_json(URL)


@respx.mock
def test_cache_avoids_second_request(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    client = make_client(FakeClock(), cache_dir=tmp_path)
    assert client.get_json(URL, params={"a": "1"}) == {"n": 1}
    assert client.get_json(URL, params={"a": "1"}) == {"n": 1}
    assert route.call_count == 1
    assert client.stats.cache_hits == 1


@respx.mock
def test_cache_separates_params(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    client = make_client(FakeClock(), cache_dir=tmp_path)
    client.get_json(URL, params={"a": "1"})
    client.get_json(URL, params={"a": "2"})
    assert route.call_count == 2


@respx.mock
def test_failures_and_bad_bodies_are_not_cached(tmp_path: Path) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, text="not json"), httpx.Response(200, json={"n": 2})]
    )
    client = make_client(FakeClock(), cache_dir=tmp_path)
    with pytest.raises(HttpFetchError):
        client.get_json(URL)
    assert client.get_json(URL) == {"n": 2}
