"""Live ingest (docs/03-data-ingestion.md section 2.4): finals, schedule and injuries.

    docker compose up -d --build live-ingest
    docker compose run --rm dev python -m hoopsedge.ingest.live status
    docker compose run --rm dev python -m hoopsedge.ingest.live clear-halt
    docker compose run --rm live-ingest python -m hoopsedge.ingest.live once --dates 2026-11-05

Files decide what has been processed, not the database, so losing the local stand-in costs
nothing. A game's `games` delta is written last and marks it done. Raw and curated files follow
docs/12's layout under the data directory, which an S3-backed store replaces at deployment (D47).

A date becomes settled when every game on it has finished: its scoreboard is then stored where
the backfill keeps them, so `backfill build` picks it up, and scanning resumes after it.
"""

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from botocore.exceptions import BotoCoreError, ClientError

from hoopsedge.common.dynamo import GameTable, connect, ensure_table, table_name
from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.common.schemas import Game, Reject
from hoopsedge.ingest.backfill import (
    LEAGUES,
    PRESEASON,
    SITE,
    Backfill,
    BlockedError,
    assert_not_halted,
    fetch_json,
    halt_on_block,
    halt_path,
    raw_path,
    read_gz,
    write_gz,
)
from hoopsedge.ingest.collector import CollectorError, Context, collect_injuries
from hoopsedge.ingest.curated import delta_path, rows_frame, write_parquet
from hoopsedge.ingest.espn_coverage import EASTERN, check_scoreboard_event, parse_time
from hoopsedge.ingest.espn_parser import POSTSEASON_TYPES, ParseResult, parse_game
from hoopsedge.ingest.injuries import sync_injuries
from hoopsedge.teams.registry import teams_from_list

USER_AGENT = "HoopsEdge-ingest/0.1 (private research client)"
INTERVAL = timedelta(minutes=30)
STALE_AFTER = timedelta(hours=2)  # Docs/11 sets this as the stale-ingest warning.
LOOKBACK_DAYS = 14
POLL_SECONDS = 30.0
DEFAULT_DATA_DIR = "/workspace/data"
FINISHED_STATE = "post"
PREGAME_STATE = "pre"
TABLES_WITH_DELTAS = ("games", "team_games", "player_games")
RECENT_RUNS = timedelta(days=1)

Log = Callable[[str], None]


@dataclass(frozen=True)
class Live:
    data_dir: Path
    client: PoliteClient
    table: GameTable
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    log: Log = print

    def today(self) -> date:
        return self.now().astimezone(EASTERN).date()

    def fetcher(self) -> Backfill:
        return Backfill(self.data_dir, self.client, self.today(), self.log)


@dataclass
class IngestResult:
    scanned: dict[str, list[str]] = field(default_factory=dict)
    new_finals: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    box_updates: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    upserts: int = 0
    injuries: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def is_halted(data_dir: Path) -> bool:
    return halt_path(data_dir).exists()


def clear_halt(data_dir: Path) -> None:
    halt_path(data_dir).unlink(missing_ok=True)


def scan_days(live: Live, league: str) -> list[date]:
    """Yesterday to tomorrow, reaching further back to any date not yet settled.

    Finals can be recovered, so days the machine was off are caught up rather than lost. With no
    settled date in the lookback, as on a first run, it goes back the full lookback only.
    """
    today = live.today()
    yesterday = today - timedelta(days=1)
    start = today - timedelta(days=LOOKBACK_DAYS)
    for back in range(1, LOOKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        if raw_path(live.data_dir, league, day, "scoreboard").exists():
            start = day + timedelta(days=1)
            break
    start = min(start, yesterday)
    return [start + timedelta(days=i) for i in range((today - start).days + 2)]


def _game_item(league: str, event: dict[str, Any], checked_venue: bool = False) -> dict[str, Any]:
    competition = event["competitions"][0]
    competitors = competition["competitors"]
    home = next(c for c in competitors if c["homeAway"] == "home")
    away = next(c for c in competitors if c["homeAway"] == "away")
    tip = parse_time(event["date"])
    slate = tip.astimezone(EASTERN).date().isoformat()
    game_id = str(event["id"])
    status = competition["status"]["type"]
    item: dict[str, Any] = {
        "pk": f"GAME#{league}#{game_id}",
        "sk": "META",
        "gsi1pk": f"SLATE#{league}#{slate}",
        "gsi1sk": tip.isoformat(),
        "league": league,
        "game_id": game_id,
        "season": int(event["season"]["year"]),
        "slate_date_et": slate,
        "tip_utc": tip.isoformat(),
        "home_id": str(home["id"]),
        "away_id": str(away["id"]),
        "status": str(status["name"]),
        "postseason": event["season"].get("type") in POSTSEASON_TYPES,
    }
    if isinstance(competition.get("neutralSite"), bool):
        item["neutral"] = competition["neutralSite"]
    venue = competition.get("venue", {}).get("id")
    if venue:
        item["venue_id"] = str(venue)
    if checked_venue:
        item["venue_checked"] = True
    if status.get("state") == FINISHED_STATE and home.get("score") is not None:
        item["home_pts"] = int(home["score"])
        item["away_pts"] = int(away["score"])
    return item


def _is_ignored(event: dict[str, Any], league: str) -> bool:
    check = check_scoreboard_event(event, league)
    return check.is_exhibition or check.season_type == PRESEASON


def _reject_path(data_dir: Path, table: str, game_id: str) -> Path:
    return data_dir / f"curated/_rejects/{table}/live/{game_id}.jsonl"


def _write_rejects(data_dir: Path, rejects: Sequence[Reject]) -> None:
    for reject in rejects:
        path = _reject_path(data_dir, reject.table, reject.game_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reject.model_dump_json() + "\n", "utf-8")


def _write_deltas(data_dir: Path, league: str, day: date, game: Game, parsed: ParseResult) -> None:
    def path(table: str) -> Path:
        return delta_path(data_dir, table, day.isoformat(), league, game.season, game.game_id)

    for table, rows in (("team_games", parsed.team_games), ("player_games", parsed.player_games)):
        if rows:
            write_parquet(rows_frame(rows), path(table))
        else:
            path(table).unlink(missing_ok=True)
    write_parquet(rows_frame([game]), path("games"))  # Written last: it marks the game done.


def _stored_row(
    data_dir: Path, league: str, day: date, event: dict[str, Any]
) -> dict[str, Any] | None:
    season = int(event["season"]["year"])
    path = delta_path(data_dir, "games", day.isoformat(), league, season, str(event["id"]))
    return pl.read_parquet(path).row(0, named=True) if path.exists() else None


def _board_scores(event: dict[str, Any]) -> tuple[int, int]:
    competitors = event["competitions"][0]["competitors"]
    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")
    return int(home["score"]), int(away["score"])


def _nba_ids(data_dir: Path) -> set[str]:
    file = data_dir / "raw/espn/registry/NBA-teams.json.gz"
    return {team.team_id for team in teams_from_list("NBA", read_gz(file))}


def _process_final(
    live: Live, league: str, day: date, event: dict[str, Any], result: IngestResult
) -> bool:
    """Process one final. Returns whether the game is usable, so it can go in the table."""
    game_id = str(event["id"])
    stored = _stored_row(live.data_dir, league, day, event)
    summary_file = raw_path(live.data_dir, league, day, game_id)
    corrected = False
    box_late = False
    if stored is not None:
        corrected = (stored["home_pts"], stored["away_pts"]) != _board_scores(event)
        box_late = bool(stored["box_missing"])
        if not corrected and not box_late:
            return True
    elif _reject_path(live.data_dir, "games", game_id).exists():
        return False

    path, _ = LEAGUES[league]
    if summary_file.exists() and not corrected and not box_late:
        summary = read_gz(summary_file)  # A crash came between the raw write and the delta.
    else:
        summary = fetch_json(live.fetcher(), f"{SITE}/{path}/summary", {"event": game_id})
        write_gz(summary_file, summary)  # Docs/03 section 2.1: raw is written before parsing.

    known = _nba_ids(live.data_dir) if league == "NBA" else None
    parsed = parse_game(league, event, summary, known)
    _write_rejects(live.data_dir, parsed.rejects)
    if parsed.game is None:
        result.rejected.append(game_id)
        return False
    if box_late and not corrected and parsed.game.box_missing:
        return True  # Still no box, so there's nothing new to say about this game.
    _write_deltas(live.data_dir, league, day, parsed.game, parsed)
    result.new_finals.append(game_id)
    if corrected:
        result.corrections.append(game_id)
    elif box_late:
        result.box_updates.append(game_id)
    return True


def _check_venue(live: Live, league: str, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fetch a summary for an upcoming game with no venue or neutral flag, once per game."""
    item = _game_item(league, event)
    if "venue_id" in item and "neutral" in item:
        return event, False
    stored = live.table.get_game(league, item["game_id"])
    if stored is not None and stored.get("venue_checked"):
        for key in ("venue_id", "neutral"):
            if key in stored:
                item.setdefault(key, stored[key])
        return _with_fields(event, item), True
    path, _ = LEAGUES[league]
    summary = fetch_json(live.fetcher(), f"{SITE}/{path}/summary", {"event": item["game_id"]})
    now = live.now()
    write_gz(
        live.data_dir
        / f"raw/espn/live/{now:%Y/%m/%d}/{league}-pregame-{item['game_id']}-{now:%H%M%S}.json.gz",
        summary,
    )
    venue = (summary.get("gameInfo") or {}).get("venue", {}).get("id")
    if venue and "venue_id" not in item:
        item["venue_id"] = str(venue)
    header = (summary.get("header") or {}).get("competitions") or [{}]
    if "neutral" not in item and isinstance(header[0].get("neutralSite"), bool):
        item["neutral"] = header[0]["neutralSite"]
    return _with_fields(event, item), True


def _with_fields(event: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    updated: dict[str, Any] = json.loads(json.dumps(event))
    competition = updated["competitions"][0]
    if "venue_id" in item:
        competition["venue"] = {"id": item["venue_id"]}
    if "neutral" in item:
        competition["neutralSite"] = item["neutral"]
    return updated


def _scan_day(live: Live, league: str, day: date, result: IngestResult) -> None:
    path, params = LEAGUES[league]
    try:
        board = fetch_json(
            live.fetcher(),
            f"{SITE}/{path}/scoreboard",
            {"dates": day.strftime("%Y%m%d"), **params},
        )
    except HttpFetchError as error:
        result.failures[f"{league} {day} scoreboard"] = str(error)
        return
    now = live.now()
    write_gz(
        live.data_dir / f"raw/espn/live/{now:%Y/%m/%d}/{league}-{day:%Y%m%d}-{now:%H%M%S}.json.gz",
        board,
    )

    failed_here = False
    all_finished = True
    for event in board.get("events", []):
        game_id = str(event.get("id"))
        try:
            state = event["competitions"][0].get("status", {}).get("type", {}).get("state")
            all_finished = all_finished and state == FINISHED_STATE
            if _is_ignored(event, league):
                continue
            usable = True
            checked = False
            if check_scoreboard_event(event, league).is_final:
                usable = _process_final(live, league, day, event, result)
            elif state == PREGAME_STATE:
                event, checked = _check_venue(live, league, event)
            if usable and live.table.upsert_game(_game_item(league, event, checked)):
                result.upserts += 1
        except HttpFetchError as error:
            failed_here = True
            result.failures[game_id] = str(error)
        except (BotoCoreError, ClientError) as error:
            failed_here = True
            result.failures[game_id] = f"database: {error}"
        except (KeyError, IndexError, TypeError, ValueError, StopIteration, OSError) as error:
            # One odd game must not stop the others. It stays visible and its date stays open.
            failed_here = True
            result.failures[game_id] = f"{type(error).__name__}: {error}"

    if day < live.today() and all_finished and not failed_here:
        write_gz(raw_path(live.data_dir, league, day, "scoreboard"), board)
        result.settled.append(f"{league} {day}")


def ingest_once(live: Live, dates: Sequence[date] | None = None) -> IngestResult:
    """One full pass, also the slate pipeline's first step. A block raises BlockedError."""
    assert_not_halted(live.data_dir)
    result = IngestResult()
    for league in LEAGUES:
        days = list(dates) if dates else scan_days(live, league)
        result.scanned[league] = [day.isoformat() for day in days]
        for day in days:
            _scan_day(live, league, day, result)
    context = Context(live.data_dir, live.client, live.client, live.now)
    try:
        collect_injuries(context)
    except HttpFetchError as error:
        halt_on_block(live.data_dir, error)
        result.failures["injuries"] = str(error)
    except (CollectorError, ValueError, KeyError, TypeError) as error:
        result.failures["injuries"] = f"{type(error).__name__}: {error}"
    result.injuries = sync_injuries(live.data_dir)
    for bad in result.injuries["bad_snapshots"]:
        result.failures[f"injury snapshot {bad}"] = "unreadable or incomplete"
    return result


def _runs_path(data_dir: Path) -> Path:
    return data_dir / "ingest" / "runs.jsonl"


def read_runs(data_dir: Path) -> list[dict[str, Any]]:
    path = _runs_path(data_dir)
    if not path.exists():
        return []
    runs: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:  # A crash can tear the last line.
            print(f"Skipping malformed log line: {line[:80]}", file=sys.stderr, flush=True)
    return runs


def _append_run(live: Live, record: dict[str, Any]) -> None:
    path = _runs_path(live.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record)
    with path.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    live.log(line)


def run_scheduled(live: Live) -> None:
    """Run if due and not halted, and record the outcome. Never raises for an ESPN failure."""
    if is_halted(live.data_dir):
        return
    runs = read_runs(live.data_dir)
    now = live.now()
    if runs and now - datetime.fromisoformat(runs[-1]["at"]) < INTERVAL:
        return
    record: dict[str, Any] = {"at": now.isoformat()}
    try:
        result = ingest_once(live)
        record.update(asdict(result))
        record["outcome"] = "failed" if result.failures else "ok"
    except BlockedError as error:
        record.update(outcome="blocked", error=str(error))
    except (BotoCoreError, ClientError) as error:
        record.update(outcome="failed", failures={"database": str(error)})
    record["seconds"] = round((live.now() - now).total_seconds(), 1)
    _append_run(live, record)


def status_report(data_dir: Path, now: datetime) -> tuple[str, bool]:
    """Return a readable report, and whether ingest is running as intended. Files only."""
    runs = read_runs(data_dir)
    lines: list[str] = []
    healthy = True
    if is_halted(data_dir):
        healthy = False
        lines.append(f"HALTED: {halt_path(data_dir).read_text('utf-8')}")
    if not runs:
        lines.append("Never run")
        return "\n".join(lines), False
    last = runs[-1]
    age = now - datetime.fromisoformat(last["at"])
    stale = age > STALE_AFTER
    healthy = healthy and last["outcome"] == "ok" and not stale
    lines.append(
        f"Last run {age.total_seconds() / 60:.0f} min ago: {last['outcome']}"
        f"{' (STALE)' if stale else ''}"
    )
    for name, reason in (last.get("failures") or {}).items():
        lines.append(f"  failure: {name}: {reason[:160]}")
    recent = [r for r in runs if now - datetime.fromisoformat(r["at"]) < RECENT_RUNS]
    finals = sum(len(r.get("new_finals") or []) for r in recent)
    corrections = sum(len(r.get("corrections") or []) for r in recent)
    rejected = sum(len(r.get("rejected") or []) for r in recent)
    problems = sum(r["outcome"] != "ok" for r in recent)
    plural = "" if finals == 1 else "s"
    lines.append(
        f"Last 24 h: {len(recent)} runs, {problems} not ok, {finals} new final{plural}, "
        f"{corrections} score corrections, {rejected} rejected"
    )
    first = datetime.fromisoformat(runs[0]["at"])
    clean = _clean_hours(runs, now)
    lines.append(
        f"Running since {first:%Y-%m-%d %H:%M} UTC. Failure-free for the last {clean:.1f} h "
        "(the Definition of Done needs 48 h)."
    )
    return "\n".join(lines), healthy


def _clean_hours(runs: Sequence[dict[str, Any]], now: datetime) -> float:
    """Hours since the last bad run, or a gap long enough to count as stale."""
    start = datetime.fromisoformat(runs[-1]["at"])
    for run in reversed(runs):
        moment = datetime.fromisoformat(run["at"])
        if run["outcome"] != "ok" or start - moment > STALE_AFTER:
            break
        start = moment
    return (now - start).total_seconds() / 3600


def _data_dir() -> Path:
    return Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))


def _build(resource: Any) -> Live:
    client = PoliteClient(USER_AGENT, rate_per_second=2.0)
    return Live(_data_dir(), client, GameTable(resource.Table(table_name())))


def run_forever(stop: threading.Event) -> None:
    resource = connect()
    live = _build(resource)
    while not stop.is_set():
        try:
            # The local stand-in loses its table whenever it restarts.
            ensure_table(resource, table_name())
            run_scheduled(live)
        except (BotoCoreError, ClientError) as error:
            live.log(json.dumps({"at": live.now().isoformat(), "database_error": str(error)}))
        stop.wait(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("status")
    commands.add_parser("clear-halt")
    once = commands.add_parser("once", help="One pass, for re-running given dates (docs/11).")
    once.add_argument("--dates", nargs="*", help="YYYY-MM-DD dates. Default: the normal scan.")
    args = parser.parse_args()

    if args.command == "run":
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        run_forever(stop)
        return 0
    if args.command == "clear-halt":
        clear_halt(_data_dir())
        print("Halt cleared.")
        return 0
    if args.command == "status":
        report, healthy = status_report(_data_dir(), datetime.now(UTC))
        print(report)
        return 0 if healthy else 1
    resource = connect()
    ensure_table(resource, table_name())
    days = [date.fromisoformat(value) for value in args.dates or []]
    result = ingest_once(_build(resource), days or None)
    print(json.dumps(asdict(result), indent=1))
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
