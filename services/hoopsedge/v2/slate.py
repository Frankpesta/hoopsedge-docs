"""v2 slate run (A3): ESPN refresh, SportyBet lines, L5 predictions, legs, dry-run tickets, CSV.

    docker compose run --rm dev python -m hoopsedge.v2.slate run --sport NCAAF --date 2026-11-28

Every run gets its own folder, `data/v2/runs/{sport}/{date}/{run_id}/`, holding the CSV in the
owner's format (brief §8.1), `run.json` with every row, leg, ticket, drop and failure, and the raw
SportyBet responses. Nothing is overwritten: a re-run is a new folder (invariant 6).

Booking is dry-run only in A3: each ticket gets a `DRYRUN-` code and its exact booking payload.
Lines are re-checked from responses no more than 5 minutes old just before the codes are made
(invariant 3), and started or soon-starting games are dropped (invariant 2, D86).
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from hoopsedge.common.config_v2 import SportConfig, V2Config, load_defaults
from hoopsedge.common.dynamo import connect, ensure_table, table_name
from hoopsedge.common.http import PoliteClient
from hoopsedge.common.ids import new_ulid
from hoopsedge.ingest.backfill import Backfill, today_eastern
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.ingest.scores import (
    SOURCES,
    USER_AGENT,
    read_teams,
    refresh,
    season_for,
    write_season,
)
from hoopsedge.l5.data import season_inputs
from hoopsedge.l5.model import GamePrediction, Result, TeamInput, fixed_tiers, predict_game
from hoopsedge.l5.model import team_input as l5_team_input
from hoopsedge.sportybet.booking import dry_run_code
from hoopsedge.sportybet.client import BlockedError, SportyBetClient, SportyBetError, build_client
from hoopsedge.v2.legs import KINDS, Kind, Leg, game_legs, recheck, team_lines
from hoopsedge.v2.lines import EventLines, LineShapeError, main_line, parse_event
from hoopsedge.v2.names import Directory, EspnGame, EventMatch, Team, match_event, seed_aliases
from hoopsedge.v2.store import add_alias, owner_aliases, save_unmatched
from hoopsedge.v2.tickets import Dropped, Ticket, build_tickets

FRESH_FOR = timedelta(minutes=5)  # Invariant 3.
SLATE_MARGIN = timedelta(hours=6)  # SportyBet's start can differ from ESPN's.
LIST_MARKETS = ["225", "227", "228"]
DEFAULT_DATA_DIR = "/workspace/data"
CSV_COLUMNS = [
    "Away", "Home", "Away Atk/Def", "Home Atk/Def", "Away L5 PF/PA", "Home L5 PF/PA",
    "Sum of Avgs", "Model Total", "Glossary Outlook", "Vegas Total", "Lean", "Kickoff (WAT)",
    "Main Line", "Booked Line", "Booked Odds", "Edge vs Main", "Edge vs Booked", "Home Proj",
    "Away Proj", "Team Pick", "Team Line", "Team Odds", "Flags",
]  # fmt: skip


class RunFailed(RuntimeError):
    """The run can't be trusted. It is marked failed, with what it had so far (invariant 7)."""


@dataclass(frozen=True)
class Snapshot:
    """One event's parsed detail and when it was fetched."""

    lines: EventLines
    fetched_at: datetime


@dataclass
class GameRow:
    event_id: str
    game_id: str
    orientation: str
    home: str
    away: str
    kickoff: datetime
    prediction: GamePrediction
    other_variant: GamePrediction  # The other `l5_same_level_only` setting, for grading.
    home_team_line: Any
    away_team_line: Any
    legs: dict[Kind, Leg]


@dataclass
class SlateBuild:
    rows: list[GameRow] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    other_slates: int = 0


def slate_bounds(slate: date) -> tuple[datetime, datetime]:
    start = datetime(slate.year, slate.month, slate.day, tzinfo=EASTERN)
    return start - SLATE_MARGIN, start + timedelta(days=1) + SLATE_MARGIN


def espn_games(results: pl.DataFrame) -> list[EspnGame]:
    return [
        EspnGame(r["game_id"], r["home_id"], r["away_id"], r["kickoff_utc"], r["neutral"])
        for r in results.iter_rows(named=True)
    ]


def directory_for(teams: pl.DataFrame, aliases: Mapping[str, str]) -> Directory:
    return Directory(
        [
            Team(r["team_id"], r["display_name"], r["short_name"], r["location"], r["mascot"],
                 r["level"])
            for r in teams.iter_rows(named=True)
        ],
        aliases,
    )  # fmt: skip


def _inputs(
    game: EspnGame,
    finals: list[Result],
    season: int,
    config: SportConfig,
    levels: dict[str, str],
    same_level_only: bool,
) -> tuple[TeamInput, TeamInput]:
    def one(team_id: str) -> TeamInput:
        return l5_team_input(
            team_id,
            finals,
            season=season,
            before=game.kickoff_utc,
            window=config.l5_window,
            levels=levels,
            same_level_only=same_level_only,
        )

    return one(game.home_id), one(game.away_id)


def build_slate(
    sport: str,
    slate: date,
    config: V2Config,
    events: Iterable[Mapping[str, Any]],
    detail: Callable[[str], Snapshot],
    results: pl.DataFrame,
    finals: list[Result],
    levels: dict[str, str],
    directory: Directory,
) -> SlateBuild:
    """Match the list's events, then predict each matched game of the slate. `detail` fetches
    an event's lines; a BlockedError from it stops everything, any other failure skips the game."""
    sport_config = config.sports[sport]  # type: ignore[index]
    tiers = fixed_tiers(sport_config)
    games = espn_games(results)
    slate_dates = dict(zip(results["game_id"], results["slate_date_et"], strict=True))
    season = int(results["season"][0])
    low, high = slate_bounds(slate)
    build = SlateBuild()
    for event in events:
        event_id = str(event["eventId"])
        start = datetime.fromtimestamp(int(event["estimateStartTime"]) / 1000, UTC)
        if not low <= start < high:
            continue
        found = match_event(
            str(event["homeTeamName"]), str(event["awayTeamName"]), start, games, directory
        )
        if not found.matched or found.game is None:
            build.unmatched.append(_unmatched(event, start, found, directory))
            continue
        game = found.game
        if slate_dates[game.game_id] != slate:
            build.other_slates += 1
            continue
        try:
            snapshot = detail(event_id)
        except BlockedError:
            raise
        except (SportyBetError, LineShapeError, KeyError, ValueError) as error:
            reason = f"{type(error).__name__}: {error}"
            build.failures.append({"event_id": event_id, "reason": reason})
            continue
        lines = snapshot.lines
        home_line, away_line = team_lines(lines, found.orientation or "")
        main = main_line(lines.totals)
        predictions = {}
        for variant in (False, True):
            home, away = _inputs(game, finals, season, sport_config, levels, variant)
            predictions[variant] = predict_game(
                home,
                away,
                config=sport_config,
                bands=config.outlook,
                tiers=tiers,
                main_line=main.line if main else None,
                home_team_line=home_line.line if home_line else None,
                away_team_line=away_line.line if away_line else None,
                levels=levels,
            )
        active = sport_config.l5_same_level_only
        names = (directory.teams[game.home_id].short_name, directory.teams[game.away_id].short_name)
        build.rows.append(
            GameRow(
                event_id=event_id,
                game_id=game.game_id,
                orientation=found.orientation or "",
                home=names[0],
                away=names[1],
                kickoff=lines.start,
                prediction=predictions[active],
                other_variant=predictions[not active],
                home_team_line=home_line,
                away_team_line=away_line,
                legs=game_legs(
                    predictions[active],
                    lines,
                    found.orientation or "",
                    names,
                    game.game_id,
                    sport_config,
                ),
            )  # fmt: skip
        )
    build.rows.sort(key=lambda row: (row.kickoff, row.event_id))
    return build


def _unmatched(
    event: Mapping[str, Any], start: datetime, found: EventMatch, directory: Directory
) -> dict[str, Any]:
    def side(name: str, resolution: Any) -> dict[str, Any]:
        return {
            "name": name,
            "team_id": resolution.team_id,
            "reason": resolution.reason,
            "suggestions": [
                {"team_id": t, "name": directory.teams[t].display_name, "score": s}
                for t, s in resolution.suggestions
            ],
        }

    return {
        "event_id": str(event["eventId"]),
        "start": start.isoformat(),
        "reason": found.reason,
        "home": side(str(event["homeTeamName"]), found.home),
        "away": side(str(event["awayTeamName"]), found.away),
    }


def recheck_legs(
    legs: Iterable[Leg],
    fresh: Callable[[str], Snapshot],
    config: SportConfig,
    now: datetime,
    cutoff: timedelta,
) -> tuple[list[Leg], list[Dropped]]:
    """Invariants 2 and 3, just before codes are made: each leg against its event's latest lines."""
    kept: list[Leg] = []
    dropped: list[Dropped] = []
    for leg in legs:
        snapshot = fresh(leg.event_id)
        if now - snapshot.fetched_at > FRESH_FOR:
            raise RunFailed(f"Lines for {leg.event_id} are older than {FRESH_FOR}")
        checked, reason = recheck(leg, snapshot.lines, config, now, cutoff)
        if checked is None:
            dropped.append(Dropped(leg, reason or "dropped"))
        else:
            kept.append(checked)
    return kept, dropped


# Output


def _one(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def _odds(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def csv_rows(build: SlateBuild, zone: ZoneInfo) -> list[dict[str, str]]:
    out = []
    for row in build.rows:
        p = row.prediction
        lean_leg = row.legs.get("TOTALS_LEAN")
        team_leg = row.legs.get("TEAM_TOTALS")
        lean = p.lean or ("SKIP" if not p.eligible else "")
        flags = list(p.flags) + ([p.skip_reason] if p.skip_reason else [])
        out.append(
            {
                "Away": row.away,
                "Home": row.home,
                "Away Atk/Def": "/".join(p.away_tiers),
                "Home Atk/Def": "/".join(p.home_tiers),
                "Away L5 PF/PA": f"{p.away.pf:.1f}/{p.away.pa:.1f}",
                "Home L5 PF/PA": f"{p.home.pf:.1f}/{p.home.pa:.1f}",
                "Sum of Avgs": _one(p.sum_of_avgs),
                "Model Total": _one(p.model_total),
                "Glossary Outlook": p.outlook,
                "Vegas Total": _one(p.main_line),
                "Lean": lean,
                "Kickoff (WAT)": row.kickoff.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                "Main Line": _one(p.main_line),
                "Booked Line": _one(lean_leg.booked_line) if lean_leg else "",
                "Booked Odds": _odds(lean_leg.odds) if lean_leg else "",
                "Edge vs Main": _one(p.edge),
                "Edge vs Booked": _one(lean_leg.edge_booked) if lean_leg else "",
                "Home Proj": _one(p.home_proj),
                "Away Proj": _one(p.away_proj),
                "Team Pick": (
                    f"{row.home if team_leg.team == 'home' else row.away} {team_leg.direction}"
                    if team_leg
                    else ""
                ),
                "Team Line": _one(team_leg.booked_line) if team_leg else "",
                "Team Odds": _odds(team_leg.odds) if team_leg else "",
                "Flags": " ".join(f"⚠ {flag}" for flag in flags),
            }
        )
    return out


def csv_text(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Can't write {type(value).__name__}")


def ticket_document(ticket: Ticket, ticket_id: str, code: str, zone: ZoneInfo) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "kind": ticket.kind,
        "number": ticket.number,
        "code": code,
        "dry_run": code.startswith("DRYRUN-"),
        "legs": len(ticket.legs),
        "total_odds": ticket.total_odds,
        "bookmaker_implied_probability": ticket.bookmaker_implied,
        "earliest_kickoff": ticket.earliest_kickoff.isoformat(),
        "earliest_kickoff_wat": ticket.earliest_kickoff.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "model_disagrees": ticket.model_disagrees,
        "selections": ticket.selections(),
        "leg_details": [asdict(leg) for leg in ticket.legs],
    }


def _row_document(row: GameRow) -> dict[str, Any]:
    def prediction(p: GamePrediction) -> dict[str, Any]:
        data = asdict(p)
        data["team_pick"] = asdict(p.team_pick) if p.team_pick else None
        return data

    return {
        "event_id": row.event_id,
        "game_id": row.game_id,
        "orientation": row.orientation,
        "home": row.home,
        "away": row.away,
        "kickoff": row.kickoff,
        "prediction": prediction(row.prediction),
        "other_variant": prediction(row.other_variant),
        "legs": {kind: asdict(leg) for kind, leg in row.legs.items()},
    }


# Running against the real services


class Runner:
    """Holds the I/O: SportyBet client, data folder, clock. Kept apart from `build_slate`."""

    def __init__(
        self,
        client: SportyBetClient,
        data_dir: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.clock = clock
        self.snapshots: dict[str, Snapshot] = {}

    def events(self, sport_config: SportConfig) -> list[dict[str, Any]]:
        """Every event in the sport's tournaments, each once; a short count fails the run."""
        seen: dict[str, dict[str, Any]] = {}
        for tournament_id in sport_config.tournament_ids:
            before, page = len(seen), 1
            while True:
                document = self.client.events_page(
                    tournament_id, page, sport_config.sport_id, LIST_MARKETS
                )
                data = document.get("data") or {}
                total = int(data.get("totalNum") or 0)
                known = len(seen)
                for group in data.get("tournaments") or []:
                    for event in group.get("events", []):
                        seen.setdefault(str(event["eventId"]), event)
                if len(seen) == known or len(seen) - before >= total:
                    break
                page += 1
            if len(seen) - before < total:
                raise RunFailed(f"{tournament_id}: got {len(seen) - before} events of {total}")
        return [seen[key] for key in sorted(seen)]

    def detail(self, event_id: str) -> Snapshot:
        document = self.client.event_detail(event_id)
        snapshot = Snapshot(parse_event(document["data"]), self.clock())
        self.snapshots[event_id] = snapshot
        return snapshot

    def fresh(self, event_id: str) -> Snapshot:
        known = self.snapshots.get(event_id)
        if known is not None and self.clock() - known.fetched_at <= FRESH_FOR - timedelta(
            minutes=1
        ):
            return known
        return self.detail(event_id)


def run_slate(
    runner: Runner,
    sport: str,
    slate: date,
    config: V2Config,
    results: pl.DataFrame,
    finals: list[Result],
    levels: dict[str, str],
    directory: Directory,
    run_dir: Path,
) -> dict[str, Any]:
    """The whole run after ESPN data is loaded. Writes into `run_dir` as it goes."""
    sport_config = config.sports[sport]  # type: ignore[index]
    zone = ZoneInfo(config.timezone_display)
    cutoff = timedelta(minutes=config.tickets.booking_cutoff_minutes)
    started = runner.clock()
    document: dict[str, Any] = {
        "run_id": run_dir.name,
        "sport": sport,
        "slate": slate.isoformat(),
        "status": "running",
        "dry_run": True,
        "started_at": started.isoformat(),
        "config": config.model_dump(by_alias=True, mode="json"),
        "config_hash": _config_hash(config),
    }
    _write_json(run_dir / "run.json", document)
    events = runner.events(sport_config)
    build = build_slate(
        sport, slate, config, events, runner.detail, results, finals, levels, directory
    )
    rows = csv_rows(build, zone)
    (run_dir / f"{sport.lower()}_predictions_{slate:%Y%m%d}.csv").write_text(
        csv_text(rows), "utf-8", newline="\n"
    )
    tickets_out: list[dict[str, Any]] = []
    dropped_out: list[dict[str, Any]] = []
    for kind in KINDS:
        if not config.tickets.kinds.get(kind):
            continue
        candidates = [row.legs[kind] for row in build.rows if kind in row.legs]
        checked, dropped = recheck_legs(
            candidates, runner.fresh, sport_config, runner.clock(), cutoff
        )
        tickets, more = build_tickets(kind, checked, config.tickets, runner.clock())
        for item in dropped + more:
            dropped_out.append({"kind": kind, "reason": item.reason, "leg": asdict(item.leg)})
        for ticket in tickets:
            ticket_id = new_ulid(runner.clock())
            tickets_out.append(ticket_document(ticket, ticket_id, dry_run_code(ticket_id), zone))
    document.update(
        {
            "status": "succeeded",
            "ended_at": runner.clock().isoformat(),
            "counts": {
                "events_listed": len(events),
                "games": len(build.rows),
                "eligible": sum(row.prediction.eligible for row in build.rows),
                "unmatched": len(build.unmatched),
                "failures": len(build.failures),
                "other_slates": build.other_slates,
                "tickets": len(tickets_out),
            },
            "rows": [_row_document(row) for row in build.rows],
            "tickets": tickets_out,
            "dropped": dropped_out,
            "unmatched": build.unmatched,
            "failures": build.failures,
        }
    )
    _write_json(run_dir / "run.json", document)
    return document


def _config_hash(config: V2Config) -> str:
    text = json.dumps(config.model_dump(by_alias=True, mode="json"), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=1, default=_jsonable), "utf-8")
    os.replace(temporary, path)


def summary_text(document: Mapping[str, Any]) -> str:
    counts = document.get("counts", {})
    lines = [
        f"{document['sport']} {document['slate']} run {document['run_id']}: {document['status']}",
        "  " + ", ".join(f"{key} {value}" for key, value in counts.items()),
    ]
    for ticket in document.get("tickets", []):
        lines.append(
            f"  {ticket['kind']} #{ticket['number']}: {ticket['legs']} legs, odds "
            f"{ticket['total_odds']}, bookmaker-implied "
            f"{100 * ticket['bookmaker_implied_probability']:.2g}%, "
            f"first kickoff {ticket['earliest_kickoff_wat']} WAT, code {ticket['code']}"
        )
    for item in document.get("unmatched", []):
        lines.append(
            f"  UNMATCHED {item['away']['name']} @ {item['home']['name']}: {item['reason']}"
        )
    for item in document.get("failures", []):
        lines.append(f"  FAILED {item['event_id']}: {item['reason']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Predict a slate and make dry-run tickets")
    run.add_argument("--sport", choices=["NCAAF"], required=True)
    run.add_argument("--date", type=date.fromisoformat, required=True, help="Eastern slate date")
    run.add_argument("--no-refresh", action="store_true", help="Skip the ESPN refresh")
    alias = commands.add_parser("alias", help="Add an owner alias: SportyBet name to ESPN team")
    alias.add_argument("--sport", choices=["NCAAF"], required=True)
    alias.add_argument("--name", required=True, help="The SportyBet name, as SportyBet writes it")
    alias.add_argument("--team-id", required=True, help="The ESPN team ID")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    resource = connect()
    ensure_table(resource, table_name())
    table = resource.Table(table_name())
    today = today_eastern()
    if args.command == "alias":
        teams = read_teams(data_dir, args.sport, season_for(today))
        known = dict(zip(teams["team_id"], teams["display_name"], strict=True))
        if args.team_id not in known:
            print(f"ESPN team {args.team_id} isn't in the {args.sport} registry", file=sys.stderr)
            return 1
        key = add_alias(table, args.sport, args.name, args.team_id, datetime.now(UTC))
        print(f"{args.sport} alias {key!r} -> {args.team_id} {known[args.team_id]}")
        return 0

    config = load_defaults()
    season = season_for(args.date)
    if not args.no_refresh:
        http = PoliteClient(USER_AGENT, rate_per_second=2.0)
        try:
            outcome = refresh(Backfill(data_dir, http, today), SOURCES[args.sport], season)
        finally:
            http.close()
        if outcome["failures"]:
            print(f"ESPN refresh failed for {sorted(outcome['failures'])}", file=sys.stderr)
            return 1
        write_season(data_dir, SOURCES[args.sport], season)
    results, finals, levels = season_inputs(data_dir, args.sport, season)
    aliases = {**seed_aliases(args.sport), **owner_aliases(table, args.sport)}
    directory = directory_for(read_teams(data_dir, args.sport, season), aliases)
    now = datetime.now(UTC)
    run_dir = data_dir / f"v2/runs/{args.sport}/{args.date}/{new_ulid(now)}"
    runner = Runner(build_client(run_dir / "raw"), data_dir)
    try:
        document = run_slate(
            runner, args.sport, args.date, config, results, finals, levels, directory, run_dir
        )
    except (SportyBetError, RunFailed, LineShapeError) as error:
        failed = json.loads((run_dir / "run.json").read_text("utf-8"))
        failed.update(
            {"status": "failed", "error": f"{type(error).__name__}: {error}",
             "ended_at": datetime.now(UTC).isoformat()}
        )  # fmt: skip
        _write_json(run_dir / "run.json", failed)
        print(f"FAILED: {failed['error']}", file=sys.stderr)
        print(f"Partial run kept in {run_dir}", file=sys.stderr)
        return 1
    for item in document["unmatched"]:
        save_unmatched(table, args.sport, item, now)
    print(summary_text(document))
    print(f"Run folder: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
