"""SportyBet's public offers and share-code endpoints (docs/07 section 6.1, D11, D30).

Reads go through the polite client, with retries. Creating a code is one POST with no retries.
There is no session here, and no way to do anything with a code beyond creating and reading it.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hoopsedge.common.http import HttpFetchError, NotJsonError, PoliteClient
from hoopsedge.ingest.backfill import write_gz

PACKAGE = Path(__file__).parent
ENDPOINTS: dict[str, Any] = yaml.safe_load((PACKAGE / "endpoints.yaml").read_text("utf-8"))
MARKETS: dict[str, Any] = yaml.safe_load((PACKAGE / "markets.yaml").read_text("utf-8"))
USER_AGENT = "HoopsEdge/0.1 (private research client)"
TIMEOUT_SECONDS = 15.0
PAGE_SIZE = 100
# 202 is CloudFront's WAF challenge, as seen from the EC2 server on 2026-11-07 (D81).
BLOCKING_STATUSES = frozenset({202, 401, 403})
PROXY_ENV = "HOOPSEDGE_SPORTYBET_PROXY"
PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


class SportyBetError(Exception):
    """A SportyBet call failed. Callers must not treat this as empty data."""


class BlockedError(SportyBetError):
    """A challenge page or a refusal. Stop and report it, and never work around it."""


class BusinessError(SportyBetError):
    """HTTP 200, but `bizCode` says it failed. SportyBet names no leg when it refuses a booking."""

    def __init__(self, call: str, code: Any, message: Any) -> None:
        super().__init__(f"{call}: bizCode {code}, {message}")
        self.code = code
        self.message = message


def all_market_ids() -> list[str]:
    return [
        str(market["market_id"])
        for family in MARKETS["families"].values()
        for market in family["markets"]
    ]


class SportyBetClient:
    def __init__(self, http: PoliteClient, raw_dir: Path | None = None) -> None:
        self._http = http
        self._raw_dir = raw_dir
        self._calls = 0

    def _keep(self, call: str, document: Any) -> None:
        """Every raw response is kept, so a bad run can be traced afterwards."""
        if self._raw_dir is None:
            return
        self._calls += 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        write_gz(self._raw_dir / f"{call}-{stamp}-{self._calls:04d}.json.gz", document)

    @staticmethod
    def _url(path: str) -> str:
        return f"{ENDPOINTS['base_url']}{path}"

    def _checked(self, call: str, document: Any) -> dict[str, Any]:
        self._keep(call, document)
        if not isinstance(document, dict):
            raise SportyBetError(f"{call}: unexpected response shape")
        if document.get("bizCode") != ENDPOINTS["success_biz_code"]:
            raise BusinessError(call, document.get("bizCode"), document.get("message"))
        return document

    def _get(self, call: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            document = self._http.get_json(self._url(path), params)
        except NotJsonError as error:
            raise BlockedError(f"{call}: {error}") from error
        except HttpFetchError as error:
            if error.status in BLOCKING_STATUSES:
                raise BlockedError(f"{call}: {error}") from error
            raise SportyBetError(f"{call}: {error}") from error
        return self._checked(call, document)

    def events_page(self, tournament_id: str, page: int) -> dict[str, Any]:
        return self._get(
            "events",
            ENDPOINTS["events"]["path"],
            {
                "sportId": ENDPOINTS["sport_id"],
                "marketId": ",".join(all_market_ids()),
                "tournamentId": tournament_id,
                "pageSize": str(PAGE_SIZE),
                "pageNum": str(page),
            },
        )

    def lookup(self, code: str) -> dict[str, Any]:
        path = ENDPOINTS["share_lookup"]["path"].format(code=code)
        return self._get("share-lookup", path)

    def create_code(self, selections: list[dict[str, str]]) -> str:
        """Create a booking code. Exactly one POST, whatever happens."""
        share = ENDPOINTS["share_create"]
        try:
            document = self._http.post_json_once(
                self._url(share["path"]), {"selections": selections}, share["headers"]
            )
        except NotJsonError as error:
            raise BlockedError(f"share-create: {error}") from error
        except HttpFetchError as error:
            if error.status in BLOCKING_STATUSES:
                raise BlockedError(f"share-create: {error}") from error
            raise SportyBetError(f"share-create: {error}") from error
        checked = self._checked("share-create", document)
        value: Any = checked
        for key in share["code_field"].split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if not value:
            raise SportyBetError("share-create: success with no code")
        return str(value)


def proxy_url() -> str | None:
    """D81: where SportyBet's requests go, if not direct. Only SportyBet's clients use it (D7).

    A malformed value stops the run rather than quietly going direct. The message leaves the
    value out, because it can hold the proxy provider's credentials.
    """
    value = os.environ.get(PROXY_ENV, "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme not in PROXY_SCHEMES or not parts.hostname or port is None:
        raise ValueError(
            f"{PROXY_ENV} must look like scheme://host:port with scheme one of "
            f"{sorted(PROXY_SCHEMES)}"
        )
    return value


def build_client(raw_dir: Path | None = None) -> SportyBetClient:
    http = PoliteClient(
        USER_AGENT,
        rate_per_second=ENDPOINTS["rate_per_second"],
        timeout=TIMEOUT_SECONDS,
        proxy=proxy_url(),
    )
    return SportyBetClient(http, raw_dir)
