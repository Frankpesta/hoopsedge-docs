"""Shared HTTP client: per-host rate limit, retries, optional on-disk cache.

Every external read goes through this client (docs/03-data-ingestion.md section 2.1).
"""

import hashlib
import json
import os
import random
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60.0


class HttpFetchError(Exception):
    """A request failed for good. Callers must not treat this as empty data."""

    def __init__(self, message: str, url: str, status: int | None = None) -> None:
        super().__init__(f"{message}: {url} (status={status})")
        self.url = url
        self.status = status


class NotJsonError(HttpFetchError):
    """A 200 response that wasn't JSON, such as a challenge page."""


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    cache_hits: int = 0
    by_status: Counter[int] = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        rate_per_second: float = 2.0,
        max_retries: int = 5,
        timeout: float = 20.0,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        proxy: str | None = None,
    ) -> None:
        """`proxy` holds a password, so it is used and never kept or shown (D81)."""
        self._interval = 1.0 / rate_per_second
        self._via = " through the SportyBet proxy" if proxy else ""
        self._max_retries = max_retries
        self._cache_dir = cache_dir
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._next_allowed: dict[str, float] = {}
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
        )
        self.stats = ClientStats()

    def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        """Return parsed JSON, or raise HttpFetchError. Never returns a partial result."""
        cache_path = self._cache_path(url, params)
        if cache_path is not None and cache_path.exists():
            self.stats.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))

        body = self._fetch(url, params)
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise NotJsonError("response was not JSON", url, 200) from exc

        if cache_path is not None:
            self._write_cache(cache_path, body)
        return parsed

    def post_json_once(self, url: str, body: Any, headers: Mapping[str, str]) -> Any:
        """One POST with no retries, because a retry could create a second real object.

        A timeout leaves the outcome unknown, and this raises rather than guessing at it.
        """
        self._wait_for_slot(url)
        self.stats.requests += 1
        try:
            response = self._http.post(url, json=body, headers=dict(headers))
        except httpx.TransportError:
            # `from None`: httpx's own message can name the proxy, password included.
            raise HttpFetchError(f"POST failed{self._via}, outcome unknown", url) from None
        self.stats.by_status[response.status_code] += 1
        if response.status_code != 200:
            raise HttpFetchError("request rejected", url, response.status_code)
        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise NotJsonError("response was not JSON", url, 200) from exc

    def close(self) -> None:
        self._http.close()

    def _fetch(self, url: str, params: Mapping[str, str] | None) -> str:
        last_status: int | None = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                self.stats.retries += 1
            self._wait_for_slot(url)
            started = self._clock()
            self.stats.requests += 1
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError:
                last_status = None
                self._sleep(self._backoff(attempt, None))
                continue

            self.stats.latencies.append(self._clock() - started)
            self.stats.by_status[response.status_code] += 1
            last_status = response.status_code

            if response.status_code == 200:
                return response.text
            if response.status_code not in RETRYABLE_STATUS:
                raise HttpFetchError("request rejected", url, response.status_code)
            self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))

        raise HttpFetchError(f"retries exhausted{self._via}", url, last_status)

    def _wait_for_slot(self, url: str) -> None:
        host = urlsplit(url).netloc
        now = self._clock()
        wait = self._next_allowed.get(host, now) - now
        if wait > 0:
            self._sleep(wait)
            now += wait
        self._next_allowed[host] = now + self._interval

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None and retry_after.isdigit():
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        ceiling: float = min(1.0 * 2**attempt, MAX_BACKOFF_SECONDS)
        return ceiling * (0.5 + self._rng() * 0.5)

    def _cache_path(self, url: str, params: Mapping[str, str] | None) -> Path | None:
        if self._cache_dir is None:
            return None
        key = json.dumps([url, sorted((params or {}).items())])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / digest[:2] / f"{digest}.json"

    @staticmethod
    def _write_cache(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
