"""Phase 0 SportyBet discovery (docs/01-phase0-feasibility.md section 0.1).

Public endpoints only. Booking codes are not bets, and this script has no way to place one.

    # Read-only: coverage, fixtures and the report.
    docker compose run --rm dev python ../scripts/sportybet_discovery.py read

    # Show what a test code would contain. Nothing is booked.
    docker compose run --rm dev python ../scripts/sportybet_discovery.py book two-leg

    # Book one owner-approved test code, then decode and compare it.
    docker compose run --rm -e DRY_RUN=false dev \
        python ../scripts/sportybet_discovery.py book two-leg --live

Every attempt is written to a ledger first, so the 5-code limit survives a crash.
"""

import argparse
import json
import os
import statistics
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

from hoopsedge.common.http import PoliteClient
from hoopsedge.sportybet.discovery import (
    Selection,
    assert_code_available,
    choose_legs,
    compare_selections,
    events_from_list,
    family_coverage,
    handicap_signs_consistent,
)

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "services" / "hoopsedge" / "sportybet"
FIXTURES = REPO / "services" / "tests" / "fixtures" / "sportybet"
REPORTS = REPO / "docs" / "reports"
RESULTS = REPORTS / "phase0-sportybet-discovery.json"
LEDGER = REPORTS / "phase0-sportybet-test-codes.json"
USER_AGENT = "HoopsEdge-probe/0.1 (private research client)"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_LEAGUES = {
    "NBA": ("nba", {}),
    "NCAA": ("mens-college-basketball", {"groups": "50", "limit": "400"}),
}
ET = ZoneInfo("America/New_York")
MIN_MINUTES_BEFORE_TIP = 20
PAGE_SIZE = "100"
FIXTURE_PAGE_SIZE = "5"
LIVE_FIXTURE_TOURNAMENTS = 3

ENDPOINTS: dict[str, Any] = yaml.safe_load((PACKAGE / "endpoints.yaml").read_text("utf-8"))
MARKETS: dict[str, Any] = yaml.safe_load((PACKAGE / "markets.yaml").read_text("utf-8"))
MARKET_IDS = [
    str(market["market_id"])
    for family in MARKETS["families"].values()
    for market in family["markets"]
]


def url_for(name: str) -> str:
    return f"{ENDPOINTS['base_url']}{ENDPOINTS[name]['path']}"


def lookup_url(code: str) -> str:
    return url_for("share_lookup").format(code=code)


def list_params(tournament_id: str, page: int, size: str = PAGE_SIZE) -> dict[str, str]:
    return {
        "sportId": ENDPOINTS["sport_id"],
        "marketId": ",".join(MARKET_IDS),
        "tournamentId": tournament_id,
        "pageSize": size,
        "pageNum": str(page),
    }


def fetch_league(client: PoliteClient, league: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for tournament_id in MARKETS["leagues"][league]["tournament_ids"]:
        page = 1
        while True:
            document = client.get_json(url_for("events"), list_params(tournament_id, page))
            found = events_from_list(document)
            events += found
            if not found or len(events) >= document["data"]["totalNum"]:
                break
            page += 1
    return sorted(events, key=lambda event: event["estimateStartTime"])


def fetch_detail(client: PoliteClient, event_id: str) -> dict[str, Any]:
    detail = ENDPOINTS["event_detail"]
    return client.get_json(
        url_for("event_detail"), {"eventId": event_id, "productId": detail["product_id_prematch"]}
    )


def et_date(event: dict[str, Any]) -> str:
    start = datetime.fromtimestamp(event["estimateStartTime"] / 1000, ET)
    return start.date().isoformat()


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def specifiers(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(market["id"]), str(market.get("specifier")))
        for market in document.get("markets", [])
        if str(market["id"]) in MARKET_IDS
    }


def handicap_check(events: list[dict[str, Any]]) -> dict[str, int]:
    family = MARKETS["families"]["handicap"]
    ids = {str(market["market_id"]) for market in family["markets"]}
    sides = {str(key): value for key, value in family["outcomes"].items()}
    lines = [m for e in events for m in e["markets"] if str(m["id"]) in ids]
    good = sum(handicap_signs_consistent(line, sides) for line in lines)
    return {"lines": len(lines), "sign_matches_specifier": good}


def espn_counts(client: PoliteClient, league: str, day: str) -> dict[str, Any]:
    path, params = ESPN_LEAGUES[league]
    url = f"{ESPN}/{path}/scoreboard"
    board = client.get_json(url, {"dates": day.replace("-", ""), **params})
    return {"date": day, "games": len(board["events"])}


def summarize_league(
    league: str, events: list[dict[str, Any]], espn_client: PoliteClient
) -> dict[str, Any]:
    by_date = Counter(et_date(event) for event in events)
    first_date = min(by_date) if by_date else None
    lines_per_event: dict[str, list[int]] = {}
    for event in events:
        counts = Counter(str(market["id"]) for market in event["markets"])
        for market_id, count in counts.items():
            lines_per_event.setdefault(market_id, []).append(count)
    return {
        "events_listed": len(events),
        "events_by_et_date": dict(sorted(by_date.items())),
        "espn_games_on_first_date": espn_counts(espn_client, league, first_date)
        if first_date
        else None,
        "event_status": dict(Counter(f"{e['status']}/{e['matchStatus']}" for e in events)),
        "booking_status": dict(Counter(str(e.get("bookingStatus")) for e in events)),
        "market_status": dict(
            Counter(f"{m['id']}/{m['status']}" for e in events for m in e["markets"])
        ),
        "median_lines_per_event": {
            market_id: statistics.median(counts) for market_id, counts in lines_per_event.items()
        },
        "coverage_all_listed": family_coverage(events, MARKET_IDS),
        "coverage_first_date": family_coverage(
            [event for event in events if et_date(event) == first_date], MARKET_IDS
        ),
        "handicap_check": handicap_check(events),
    }


def record_read_fixtures(client: PoliteClient, leagues: dict[str, list[dict[str, Any]]]) -> dict:
    """Save small complete responses of each read type, and compare list with detail."""
    for league, page in (("nba", 1), ("ncaa", 1), ("ncaa", 2)):
        tournament_id = MARKETS["leagues"][league.upper()]["tournament_ids"][0]
        document = client.get_json(
            url_for("events"), list_params(tournament_id, page, FIXTURE_PAGE_SIZE)
        )
        write_json(FIXTURES / f"events-{league}-page{page}.json", document)

    comparisons = []
    earliest = {
        league: [e for e in events if et_date(e) == min(et_date(x) for x in events)]
        for league, events in leagues.items()
    }
    picks = [
        ("nba", earliest["NBA"][0]),
        ("ncaa", earliest["NCAA"][0]),
        ("ncaa", earliest["NCAA"][-1]),
    ]
    for index, (league, event) in enumerate(picks):
        document = fetch_detail(client, str(event["eventId"]))
        write_json(FIXTURES / f"event-detail-{league}-{index}.json", document)
        in_list, in_detail = specifiers(event), specifiers(document["data"])
        comparisons.append(
            {
                "event_id": event["eventId"],
                "lines_in_list": len(in_list),
                "lines_in_detail": len(in_detail),
                "only_in_detail": len(in_detail - in_list),
                "only_in_list": len(in_list - in_detail),
            }
        )

    live = client.get_json(url_for("live_events"), {"sportId": ENDPOINTS["sport_id"]})
    all_rows = live["data"]
    status_counts = Counter(
        f"{e.get('status')}/{e.get('matchStatus')}" for row in all_rows for e in row["events"]
    )
    write_json(
        FIXTURES / "live-events-trimmed.json",
        {**live, "data": all_rows[:LIVE_FIXTURE_TOURNAMENTS]},
    )
    invalid = client.get_json(lookup_url("ZZZZZZ"))
    write_json(FIXTURES / "share-lookup-invalid.json", invalid)
    return {"list_vs_detail": comparisons, "live_event_status": dict(status_counts)}


def run_read() -> None:
    client = PoliteClient(USER_AGENT, rate_per_second=ENDPOINTS["rate_per_second"], timeout=30.0)
    espn_client = PoliteClient(USER_AGENT, rate_per_second=2.0)
    try:
        leagues = {league: fetch_league(client, league) for league in MARKETS["leagues"]}
        results: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            **{
                league: summarize_league(league, events, espn_client)
                for league, events in leagues.items()
            },
            **record_read_fixtures(client, leagues),
        }
    finally:
        client.close()
        espn_client.close()
    latencies = sorted(client.stats.latencies)
    results["client"] = {
        "requests": client.stats.requests,
        "retries": client.stats.retries,
        "by_status": {str(k): v for k, v in client.stats.by_status.items()},
        "median_latency_seconds": round(latencies[len(latencies) // 2], 3) if latencies else None,
    }
    write_json(RESULTS, results)
    print(f"Results written to {RESULTS}")


def plan_code(name: str, client: PoliteClient) -> list[Selection]:
    """Choose fresh legs for one named test code."""
    earliest = int(
        (datetime.now(UTC) + timedelta(minutes=MIN_MINUTES_BEFORE_TIP)).timestamp() * 1000
    )
    nba = fetch_league(client, "NBA")
    ncaa = fetch_league(client, "NCAA")
    ids = {
        family: [str(market["market_id"]) for market in details["markets"]]
        for family, details in MARKETS["families"].items()
    }
    if name == "two-leg":
        return choose_legs(nba, [*ids["handicap"], *ids["total_ft"]], 2, earliest)
    if name == "all-markets":
        return choose_legs(ncaa, MARKET_IDS, len(MARKET_IDS), earliest)
    if name == "twenty-legs":
        return choose_legs(ncaa, MARKET_IDS, 20, earliest)
    if name == "same-event":
        first = choose_legs(ncaa, ids["total_ft"], 1, earliest)[0]
        event = [e for e in ncaa if e["eventId"] == first["eventId"]]
        return [first, choose_legs(event, ids["total_1h"], 1, earliest)[0]]
    if name == "bad-outcome":
        # A valid leg plus a real market with an outcome ID that doesn't exist, to record
        # how a rejection looks. Docs/07 section 6.4 depends on this shape.
        good = choose_legs(ncaa, ids["total_ft"], 2, earliest)
        return [good[0], {**good[1], "outcomeId": "99999"}]
    raise ValueError(f"Unknown test code: {name}")


def read_ledger() -> list[dict[str, Any]]:
    return json.loads(LEDGER.read_text("utf-8")) if LEDGER.exists() else []


def run_book(name: str, live: bool) -> None:
    client = PoliteClient(USER_AGENT, rate_per_second=ENDPOINTS["rate_per_second"], timeout=30.0)
    try:
        legs = plan_code(name, client)
        print(json.dumps({"code": name, "legs": legs}, indent=1))
        if not live:
            print("Plan only. Nothing was booked.")
            return
        if os.environ.get("DRY_RUN", "true").lower() != "false":
            raise SystemExit("Refusing to book: DRY_RUN must be false for a live test code.")

        attempts = read_ledger()
        if any(attempt["name"] == name and attempt.get("code") for attempt in attempts):
            raise SystemExit(f"Refusing to book: {name} already has a code.")
        assert_code_available(attempts)
        attempt: dict[str, Any] = {
            "name": name,
            "legs": legs,
            "at": datetime.now(UTC).isoformat(),
            "code": None,
            "outcome": "pending",
        }
        write_json(LEDGER, [*attempts, attempt])

        share = ENDPOINTS["share_create"]
        # One POST and no retries: a retry could create a second real code.
        response = httpx.post(
            url_for("share_create"),
            json={"selections": legs},
            headers={"User-Agent": USER_AGENT, **share["headers"]},
            timeout=15.0,
        )
        attempt["http_status"] = response.status_code
        write_json(FIXTURES / f"share-create-{name}.json", response.json())
        body = response.json()
        if body.get("bizCode") != ENDPOINTS["success_biz_code"]:
            attempt.update(
                outcome="rejected", biz_code=body.get("bizCode"), message=body.get("message")
            )
            write_json(LEDGER, [*attempts, attempt])
            print(f"Rejected: {body.get('bizCode')} {body.get('message')}")
            return

        code = str(body["data"]["shareCode"])
        attempt.update(code=code, outcome="created")
        write_json(LEDGER, [*attempts, attempt])
        lookup = client.get_json(lookup_url(code))
        write_json(FIXTURES / f"share-lookup-{name}.json", lookup)
        difference = compare_selections(legs, lookup)
        attempt.update(
            verified=not difference["missing"] and not difference["extra"],
            lookup_biz_code=lookup.get("bizCode"),
            difference=difference,
        )
        write_json(LEDGER, [*attempts, attempt])
        print(json.dumps({key: attempt[key] for key in ("code", "verified", "difference")}))
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read")
    book = commands.add_parser("book")
    book.add_argument(
        "name", choices=["two-leg", "all-markets", "twenty-legs", "same-event", "bad-outcome"]
    )
    book.add_argument("--live", action="store_true", help="Create the code. Owner approval first.")
    args = parser.parse_args()
    started = time.monotonic()
    if args.command == "read":
        run_read()
    else:
        run_book(args.name, args.live)
    print(f"Finished in {time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
