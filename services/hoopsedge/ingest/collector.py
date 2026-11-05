"""Early data collector (D13): snapshots data that can't be recovered later.

Runs as a local Docker service until AWS ingest takes over. Files follow the raw layout in
docs/12-data-model.md, gzipped, under the data directory. Every attempt is logged to
collector/runs.jsonl, which also drives scheduling, so a restart keeps the intervals.

    docker compose up -d --build collector
    docker compose run --rm dev python -m hoopsedge.ingest.collector status
    docker compose run --rm dev python -m hoopsedge.ingest.collector resume sportybet

A 401, 403, 451 or non-JSON response halts that source until the owner resumes it. The
collector doesn't try to get past a block.
"""

import argparse
import gzip
import json
import os
import signal
import sys
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from hoopsedge.common.http import HttpFetchError, NotJsonError, PoliteClient
from hoopsedge.sportybet.discovery import events_from_list, family_coverage

PACKAGE = Path(__file__).resolve().parent.parent / "sportybet"
ENDPOINTS: dict[str, Any] = yaml.safe_load((PACKAGE / "endpoints.yaml").read_text("utf-8"))
MARKETS: dict[str, Any] = yaml.safe_load((PACKAGE / "markets.yaml").read_text("utf-8"))
MARKET_IDS = [
    str(market["market_id"])
    for family in MARKETS["families"].values()
    for market in family["markets"]
]

SPORTYBET_BASE: str = ENDPOINTS["base_url"]
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"
ESPN_LEAGUES: dict[str, tuple[str, dict[str, str]]] = {
    "NBA": ("nba", {}),
    "NCAA": ("mens-college-basketball", {"groups": "50", "limit": "400"}),
}
USER_AGENT = "HoopsEdge-collector/0.1 (private research client)"
EASTERN = ZoneInfo("America/New_York")
DEFAULT_DATA_DIR = "/workspace/data"
PAGE_SIZE = "100"
POLL_SECONDS = 30.0
BLOCK_STATUS = frozenset({401, 403, 451})
GROWTH_ROWS = 8


class CollectorError(RuntimeError):
    """A response arrived but doesn't look as expected."""


@dataclass(frozen=True)
class Context:
    data_dir: Path
    sportybet: PoliteClient
    espn: PoliteClient
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class Job:
    name: str
    source: str
    interval: timedelta
    run: Callable[[Context], dict[str, Any]]


def et_date(moment: datetime) -> date:
    return moment.astimezone(EASTERN).date()


def write_snapshot(
    context: Context,
    directory: str,
    call: str,
    responses: Sequence[Any],
    started_at: datetime,
    complete: bool,
    dated_folders: bool = True,
) -> Path:
    """Write one gzipped snapshot atomically. `captured_at` is taken after the last response."""
    captured_at = context.now()
    folder = context.data_dir / directory
    if dated_folders:
        folder = folder / captured_at.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{call}-{captured_at:%Y%m%dT%H%M%SZ}.json.gz"
    document = {
        "started_at": started_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "complete": complete,
        "responses": list(responses),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(gzip.compress(json.dumps(document).encode("utf-8")))
    os.replace(temporary, path)
    return path


def collect_injuries(context: Context) -> dict[str, Any]:
    started = context.now()
    document = context.espn.get_json(f"{ESPN_SITE}/nba/injuries")
    write_snapshot(context, "raw/espn/injuries", "injuries", [document], started, True)
    teams = document.get("injuries") if isinstance(document, dict) else None
    if not isinstance(teams, list):
        raise CollectorError("NBA injuries response has no injuries list")
    return {
        "teams": len(teams),
        "rows": sum(len(team.get("injuries", [])) for team in teams),
    }


def _offer_pages(context: Context, tournament_id: str, pages: list[Any]) -> list[dict[str, Any]]:
    """Fetch every page, adding each response to `pages` before it is checked."""
    events: list[dict[str, Any]] = []
    page = 1
    while True:
        document = context.sportybet.get_json(
            f"{SPORTYBET_BASE}{ENDPOINTS['events']['path']}",
            {
                "sportId": ENDPOINTS["sport_id"],
                "marketId": ",".join(MARKET_IDS),
                "tournamentId": tournament_id,
                "pageSize": PAGE_SIZE,
                "pageNum": str(page),
            },
        )
        pages.append(document)
        if document.get("bizCode") != ENDPOINTS["success_biz_code"]:
            raise CollectorError(f"SportyBet bizCode {document.get('bizCode')}")
        found = events_from_list(document)
        events += found
        if not found or len(events) >= document["data"]["totalNum"]:
            return events
        page += 1


def _offer_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = family_coverage(events, MARKET_IDS)
    by_date = Counter(
        et_date(datetime.fromtimestamp(event["estimateStartTime"] / 1000, UTC)).isoformat()
        for event in events
    )
    return {
        "events": len(events),
        "by_et_date": dict(sorted(by_date.items())),
        "bookable": {
            market_id: row["events_bookable"] for market_id, row in coverage["markets"].items()
        },
        "all_markets_bookable": coverage["events_all_markets_bookable"],
    }


def collect_offers(context: Context) -> dict[str, Any]:
    started = context.now()
    directory = f"raw/sportybet/collector-{started:%Y%m%d}"
    summary: dict[str, Any] = {}
    for league, config in MARKETS["leagues"].items():
        pages: list[Any] = []
        complete = False
        events: list[dict[str, Any]] = []
        try:
            for tournament_id in config["tournament_ids"]:
                events += _offer_pages(context, tournament_id, pages)
            complete = True
        finally:
            if pages:
                write_snapshot(
                    context, directory, f"offers-{league}", pages, started, complete, False
                )
        summary[league] = _offer_summary(events)
    return summary


def _core_odds(context: Context, path: str, game_id: str) -> dict[str, Any]:
    url = f"{ESPN_CORE}/{path}/events/{game_id}/competitions/{game_id}/odds"
    try:
        document: dict[str, Any] = context.espn.get_json(url, {"limit": "50"})
    except HttpFetchError as error:
        if error.status == 404:  # ESPN holds no odds for this game.
            return {"items": []}
        raise
    return document


def collect_lines(context: Context) -> dict[str, Any]:
    """Store today's schedule and pre-game lines. D27 excludes in-play feeds."""
    summary: dict[str, Any] = {}
    for league, (path, params) in ESPN_LEAGUES.items():
        started = context.now()
        board = context.espn.get_json(
            f"{ESPN_SITE}/{path}/scoreboard",
            {"dates": et_date(started).strftime("%Y%m%d"), **params},
        )
        write_snapshot(context, "raw/espn/lines", f"{league}-scoreboard", [board], started, True)
        pre_game = [
            str(event["id"])
            for event in board["events"]
            if event["status"]["type"]["state"] == "pre"
        ]
        odds: dict[str, Any] = {}
        complete = False
        try:
            for game_id in pre_game:
                odds[game_id] = _core_odds(context, path, game_id)
            complete = True
        finally:
            if pre_game:
                write_snapshot(
                    context, "raw/espn/lines", f"{league}-odds", [odds], started, complete
                )
        summary[league] = {
            "games": len(board["events"]),
            "pre_game": len(pre_game),
            "with_odds": sum(bool(document.get("items")) for document in odds.values()),
        }
    return summary


def build_jobs() -> list[Job]:
    return [
        Job("sportybet_offers", "sportybet", timedelta(minutes=30), collect_offers),
        Job("nba_injuries", "espn", timedelta(minutes=60), collect_injuries),
        Job("espn_lines", "espn", timedelta(hours=6), collect_lines),
    ]


def _runs_path(data_dir: Path) -> Path:
    return data_dir / "collector" / "runs.jsonl"


def read_runs(data_dir: Path) -> list[dict[str, Any]]:
    path = _runs_path(data_dir)
    if not path.exists():
        return []
    runs: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:  # A crash can tear the last line; the next attempt is logged normally.
            print(f"Skipping malformed log line: {line[:80]}", file=sys.stderr, flush=True)
    return runs


def _append_run(context: Context, record: dict[str, Any]) -> None:
    path = _runs_path(context.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record)
    with path.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    print(line, flush=True)


def halted_sources(runs: Sequence[dict[str, Any]]) -> set[str]:
    halted: set[str] = set()
    for run in runs:
        if run["outcome"] == "blocked":
            halted.add(run["source"])
        elif run["outcome"] == "resumed":
            halted.discard(run["source"])
    return halted


def resume_source(context: Context, source: str) -> None:
    _append_run(
        context,
        {"job": "resume", "source": source, "outcome": "resumed", "at": context.now().isoformat()},
    )


def _is_block(error: HttpFetchError) -> bool:
    return isinstance(error, NotJsonError) or error.status in BLOCK_STATUS


def _run_job(job: Job, context: Context) -> None:
    started = context.now()
    record: dict[str, Any] = {"job": job.name, "source": job.source, "at": started.isoformat()}
    try:
        record["summary"] = job.run(context)
        record["outcome"] = "ok"
    except HttpFetchError as error:
        record["outcome"] = "blocked" if _is_block(error) else "failed"
        record["error"] = str(error)
    except (CollectorError, KeyError, TypeError, ValueError, AttributeError) as error:
        record["outcome"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
    record["seconds"] = round((context.now() - started).total_seconds(), 1)
    _append_run(context, record)


def run_due(jobs: Sequence[Job], context: Context) -> None:
    """Run each job whose interval has passed, skipping halted sources."""
    for job in jobs:
        runs = read_runs(context.data_dir)
        if job.source in halted_sources(runs):
            continue
        attempts = [run["at"] for run in runs if run["job"] == job.name]
        if attempts and context.now() - datetime.fromisoformat(attempts[-1]) < job.interval:
            continue
        _run_job(job, context)


def _folder_size(folder: Path) -> tuple[int, int]:
    sizes = [path.stat().st_size for path in folder.rglob("*.json.gz")]
    return len(sizes), sum(sizes)


def status_report(context: Context) -> tuple[str, bool]:
    """Return a readable report, and whether everything is running as intended."""
    runs = read_runs(context.data_dir)
    now = context.now()
    halted = halted_sources(runs)
    lines = []
    healthy = not halted
    for source in sorted(halted):
        lines.append(f"HALTED: {source} returned a block. Review it, then run: resume {source}")
    for job in build_jobs():
        own = [run for run in runs if run["job"] == job.name]
        if not own:
            lines.append(f"{job.name}: never run")
            healthy = False
            continue
        last = own[-1]
        age = now - datetime.fromisoformat(last["at"])
        recent = [r for r in own if now - datetime.fromisoformat(r["at"]) < timedelta(days=1)]
        problems = sum(run["outcome"] != "ok" for run in recent)
        stale = age > job.interval * 2
        healthy = healthy and last["outcome"] == "ok" and not stale
        lines.append(
            f"{job.name}: last {last['outcome']} {age.total_seconds() / 60:.0f} min ago"
            f"{' (STALE)' if stale else ''}; {len(recent)} runs in 24 h, {problems} not ok"
        )
        if last["outcome"] != "ok":
            lines.append(f"  error: {last.get('error')}")
    count, size = _folder_size(context.data_dir / "raw")
    lines.append(f"Stored: {count} snapshots, {size / 1_000_000:.1f} MB")

    today = et_date(now).isoformat()
    offers = [r for r in runs if r["job"] == "sportybet_offers" and r["outcome"] == "ok"]
    for league in MARKETS["leagues"]:
        lines.append(f"{league} listed for {today} (capture UTC: events, all markets bookable):")
        rows = [
            r
            for r in offers
            if r["summary"][league]["by_et_date"].get(today) is not None
            and et_date(datetime.fromisoformat(r["at"])).isoformat() == today
        ]
        for run in rows[-GROWTH_ROWS:]:
            lines.append(
                f"  {run['at'][11:16]}: {run['summary'][league]['by_et_date'][today]}, "
                f"{run['summary'][league]['all_markets_bookable']}"
            )
    return "\n".join(lines), healthy


def _context() -> Context:
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    return Context(
        data_dir,
        PoliteClient(USER_AGENT, rate_per_second=ENDPOINTS["rate_per_second"], timeout=30.0),
        PoliteClient(USER_AGENT, rate_per_second=2.0),
    )


def run_forever(context: Context, stop: threading.Event) -> None:
    jobs = build_jobs()
    while not stop.is_set():
        run_due(jobs, context)
        stop.wait(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("status")
    resume = commands.add_parser("resume")
    resume.add_argument("source", choices=sorted({job.source for job in build_jobs()}))
    args = parser.parse_args()

    context = _context()
    try:
        if args.command == "status":
            report, healthy = status_report(context)
            print(report)
            return 0 if healthy else 1
        if args.command == "resume":
            resume_source(context, args.source)
            return 0
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        run_forever(context, stop)
        return 0
    finally:
        context.sportybet.close()
        context.espn.close()


if __name__ == "__main__":
    sys.exit(main())
