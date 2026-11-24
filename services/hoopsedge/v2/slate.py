"""v2 slate run: ESPN refresh, SportyBet lines, L5 predictions, legs, tickets, codes, CSV.

    docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.slate \
        run --sport NCAAB --date 2026-11-24
    ... rebook --sport NCAAB --date 2026-11-24 --ticket-id <id>

Every run gets its own folder, `data/v2/runs/{sport}/{date}/{run_id}/`, holding the CSV in the
owner's format (brief §8.1), `run.json` with every row, leg, ticket, drop and failure, and the raw
SportyBet responses. Nothing is overwritten: a re-run is a new folder (invariant 6), and the
slate's `index.json` records what superseded or rebooked what (`v2.booking`).

Lines are re-checked from responses no more than 5 minutes old just before each code is made
(invariant 3), and started or soon-starting games are dropped (invariant 2, D86). Codes are
practice `DRYRUN-` codes unless both switches allow real ones (D97, `common.booking_mode`); real
codes are read back and verified (invariant 4).
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from hoopsedge.common.booking_mode import is_dry_run
from hoopsedge.common.config_v2 import SportConfig, TierThresholds, V2Config
from hoopsedge.common.dynamo import connect, ensure_table, table_name
from hoopsedge.common.http import PoliteClient
from hoopsedge.common.ids import new_ulid
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.ingest.espn_files import Fetcher, today_eastern
from hoopsedge.ingest.scores import (
    SOURCES,
    USER_AGENT,
    read_teams,
    refresh,
    season_for,
    write_season,
)
from hoopsedge.l5.data import season_inputs
from hoopsedge.l5.model import (
    GamePrediction,
    Result,
    TeamInput,
    fixed_tiers,
    predict_game,
    provisional_tiers,
)
from hoopsedge.l5.model import team_input as l5_team_input
from hoopsedge.sportybet.client import BlockedError, SportyBetClient, SportyBetError, build_client
from hoopsedge.v2.booking import (
    DRY_RUN,
    Booked,
    Booker,
    Recheck,
    book_ticket,
    leg_document,
    leg_from_document,
    read_index,
    record_run,
)
from hoopsedge.v2.config_store import load as load_config
from hoopsedge.v2.legs import KINDS, Kind, Leg, game_legs, recheck, team_ladders, team_lines
from hoopsedge.v2.lines import EventLines, LineShapeError, main_line, parse_event
from hoopsedge.v2.names import Directory, EspnGame, EventMatch, Team, match_event, seed_aliases
from hoopsedge.v2.store import add_alias, alert, owner_aliases, save_unmatched
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
    team_ladders: tuple[tuple[Any, ...], tuple[Any, ...]] = ((), ())  # ESPN home, away.


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


def tiers_for(
    sport: str,
    slate: date,
    config: SportConfig,
    finals: list[Result],
    season: int,
    levels: dict[str, str],
) -> TierThresholds:
    """Fixed tiers from config, or this slate's provisional ones: percentiles over the main
    level's teams, from games before the slate's Eastern day began (brief §4.4)."""
    if not isinstance(config.tiers, str):
        return fixed_tiers(config)
    before = datetime(slate.year, slate.month, slate.day, tzinfo=EASTERN)
    main = SOURCES[sport].main_level
    inputs = [
        l5_team_input(
            team_id, finals, season=season, before=before, window=config.l5_window,
            levels=levels, same_level_only=config.l5_same_level_only,
        )
        for team_id, level in sorted(levels.items())
        if level == main
    ]  # fmt: skip
    return provisional_tiers(inputs, config.min_games)


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
    season = int(results["season"][0])
    tiers = tiers_for(sport, slate, sport_config, finals, season, levels)
    games = espn_games(results)
    slate_dates = dict(zip(results["game_id"], results["slate_date_et"], strict=True))
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
                team_ladders=team_ladders(lines, found.orientation or ""),
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


def ticket_document(
    ticket: Ticket,
    ticket_id: str,
    booked: Booked,
    zone: ZoneInfo,
    rebook_of: str | None = None,
) -> dict[str, Any]:
    """The ticket as booked (after its final re-check), or as built if nothing was left."""
    final = booked.ticket or ticket
    return {
        "ticket_id": ticket_id,
        "kind": final.kind,
        "number": final.number,
        "status": booked.status,
        "code": booked.code,
        "reason": booked.reason,
        "dry_run": booked.status == DRY_RUN,
        "rebook_of": rebook_of,
        "legs": len(final.legs),
        "total_odds": final.total_odds,
        "bookmaker_implied_probability": final.bookmaker_implied,
        "earliest_kickoff": final.earliest_kickoff.isoformat(),
        "earliest_kickoff_wat": final.earliest_kickoff.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "model_disagrees": final.model_disagrees,
        "selections": final.selections(),
        "leg_details": [leg_document(leg) for leg in final.legs],
        "dropped_at_booking": [
            {"reason": item.reason, "leg": leg_document(item.leg)} for item in booked.dropped
        ],
        "verification": {"missing": [list(k) for k in booked.missing],
                         "extra": [list(k) for k in booked.extra]},
    }  # fmt: skip


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
        # Both teams' main team-total lines, and their ladders' ends (the lowest-odds Over and
        # Under, D101), so grading can pick for either variant (brief §9).
        "team_lines": {"home": _team_document(row.home_team_line, row.team_ladders[0]),
                       "away": _team_document(row.away_team_line, row.team_ladders[1])},
    }  # fmt: skip


def _line_document(line: Any) -> dict[str, float] | None:
    if line is None:
        return None
    return {"line": line.line, "over_odds": line.over_odds, "under_odds": line.under_odds}


def _team_document(main: Any, ladder: tuple[Any, ...]) -> dict[str, Any] | None:
    document: dict[str, Any] | None = _line_document(main)
    if document is not None and ladder:
        document["lowest"] = _line_document(ladder[0])
        document["highest"] = _line_document(ladder[-1])
    return document


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
    booker: Booker,
    trigger: str = "manual",
    config_version: int = 0,
) -> dict[str, Any]:
    """The whole run after ESPN data is loaded. Writes into `run_dir` as it goes, then records
    its tickets in the slate's index (superseding earlier real codes when it books real ones)."""
    sport_config = config.sports[sport]  # type: ignore[index]
    zone = ZoneInfo(config.timezone_display)
    cutoff = timedelta(minutes=config.tickets.booking_cutoff_minutes)
    started = runner.clock()
    document: dict[str, Any] = {
        "run_id": run_dir.name,
        "sport": sport,
        "slate": slate.isoformat(),
        "status": "running",
        "trigger": trigger,
        "dry_run": not booker.live,
        "started_at": started.isoformat(),
        "config": config.model_dump(by_alias=True, mode="json"),
        "config_version": config_version,
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
            dropped_out.append({"kind": kind, "reason": item.reason, "leg": leg_document(item.leg)})
        for ticket in tickets:
            ticket_id = new_ulid(runner.clock())
            booked = book_ticket(
                booker, ticket, ticket_id, final_recheck(runner, sport_config, cutoff),
                config.tickets.min_legs,
            )  # fmt: skip
            tickets_out.append(ticket_document(ticket, ticket_id, booked, zone))
    document.update(
        {
            "status": "failed" if booker.stopped else "succeeded",
            "error": booker.stopped,
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
    record_run(run_dir.parent, run_dir.name, trigger, tickets_out)
    return document


def final_recheck(runner: "Runner", config: SportConfig, cutoff: timedelta) -> Recheck:
    """The last look before a code is made: lines no more than 5 minutes old (invariant 3)."""

    def check(legs: Sequence[Leg]) -> tuple[list[Leg], list[Dropped]]:
        return recheck_legs(legs, runner.fresh, config, runner.clock(), cutoff)

    return check


def rebook(
    runner: "Runner",
    slate_dir: Path,
    ticket_id: str,
    config: V2Config,
    sport: str,
    booker: Booker,
    run_dir: Path,
) -> dict[str, Any]:
    """Brief §7: drop started legs, re-check the rest against fresh lines, book a new code, and
    link the two tickets. The old ticket and its run stay as they were."""
    entry = read_index(slate_dir)["tickets"].get(ticket_id)
    if entry is None:
        raise RunFailed(f"No ticket {ticket_id} on this slate")
    old_run = json.loads((slate_dir / entry["run_id"] / "run.json").read_text("utf-8"))
    old = next(t for t in old_run["tickets"] if t["ticket_id"] == ticket_id)
    legs = tuple(leg_from_document(item) for item in old["leg_details"])
    ticket = Ticket(old["kind"], old["number"], legs)
    sport_config = config.sports[sport]  # type: ignore[index]
    cutoff = timedelta(minutes=config.tickets.booking_cutoff_minutes)
    new_id = new_ulid(runner.clock())
    booked = book_ticket(
        booker, ticket, new_id, final_recheck(runner, sport_config, cutoff),
        config.tickets.min_legs,
    )  # fmt: skip
    zone = ZoneInfo(config.timezone_display)
    document: dict[str, Any] = {
        "run_id": run_dir.name,
        "sport": sport,
        "slate": slate_dir.name,
        "trigger": "rebook",
        "dry_run": not booker.live,
        "status": "failed" if booker.stopped else "succeeded",
        "error": booker.stopped,
        "started_at": runner.clock().isoformat(),
        "config_hash": _config_hash(config),
        "rebook_of": ticket_id,
        "tickets": [ticket_document(ticket, new_id, booked, zone, rebook_of=ticket_id)],
    }
    _write_json(run_dir / "run.json", document)
    record_run(slate_dir, run_dir.name, "rebook", document["tickets"], rebook_of=ticket_id)
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
            f"first kickoff {ticket['earliest_kickoff_wat']} WAT, {ticket['status']} "
            f"{ticket['code'] or ''} {ticket.get('reason') or ''}".rstrip()
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
    run = commands.add_parser("run", help="Predict a slate and book its tickets")
    run.add_argument("--sport", choices=["NCAAF", "NCAAB", "NBA"], required=True)
    run.add_argument("--date", type=date.fromisoformat, required=True, help="Eastern slate date")
    run.add_argument("--no-refresh", action="store_true", help="Skip the ESPN refresh")
    run.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "dashboard"])
    again = commands.add_parser("rebook", help="Rebook one ticket without its started legs")
    again.add_argument("--sport", choices=["NCAAF", "NCAAB", "NBA"], required=True)
    again.add_argument("--date", type=date.fromisoformat, required=True, help="Eastern slate date")
    again.add_argument("--ticket-id", required=True)
    alias = commands.add_parser("alias", help="Add an owner alias: SportyBet name to ESPN team")
    alias.add_argument("--sport", choices=["NCAAF", "NCAAB", "NBA"], required=True)
    alias.add_argument("--name", required=True, help="The SportyBet name, as SportyBet writes it")
    alias.add_argument("--team-id", required=True, help="The ESPN team ID")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    resource = connect()
    ensure_table(resource, table_name())
    table = resource.Table(table_name())
    today = today_eastern()
    if args.command == "alias":
        teams = read_teams(data_dir, args.sport, season_for(today, SOURCES[args.sport]))
        known = dict(zip(teams["team_id"], teams["display_name"], strict=True))
        if args.team_id not in known:
            print(f"ESPN team {args.team_id} isn't in the {args.sport} registry", file=sys.stderr)
            return 1
        key = add_alias(table, args.sport, args.name, args.team_id, datetime.now(UTC))
        print(f"{args.sport} alias {key!r} -> {args.team_id} {known[args.team_id]}")
        return 0

    config_version, config = load_config(table)  # What the dashboard last saved (D98).
    now = datetime.now(UTC)
    slate_dir = data_dir / f"v2/runs/{args.sport}/{args.date}"
    run_dir = slate_dir / new_ulid(now)
    runner = Runner(build_client(run_dir / "raw"), data_dir)
    live = not is_dry_run(config.dry_run)

    def raise_alert(severity: str, key: str, message: str) -> None:
        alert(table, datetime.now(UTC), severity, key, message, run_dir.name)

    booker = Booker(runner.client if live else None, live, raise_alert)
    if args.command == "rebook":
        try:
            document = rebook(
                runner, slate_dir, args.ticket_id, config, args.sport, booker, run_dir
            )
        except (SportyBetError, RunFailed, LineShapeError) as error:
            raise_alert("CRITICAL", "rebook-failed", f"{type(error).__name__}: {error}")
            print(f"FAILED: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(summary_text(document))
        return 1 if document["status"] == "failed" else 0

    season = season_for(args.date, SOURCES[args.sport])
    if not args.no_refresh:
        http = PoliteClient(USER_AGENT, rate_per_second=2.0)
        try:
            outcome = refresh(Fetcher(data_dir, http, today), SOURCES[args.sport], season)
        finally:
            http.close()
        if outcome["failures"]:
            message = f"ESPN refresh failed for {sorted(outcome['failures'])}"
            raise_alert("CRITICAL", "run-failed", f"{args.sport} {args.date}: {message}")
            print(message, file=sys.stderr)
            return 1
        write_season(data_dir, SOURCES[args.sport], season)
    results, finals, levels = season_inputs(data_dir, args.sport, season)
    aliases = {**seed_aliases(args.sport), **owner_aliases(table, args.sport)}
    directory = directory_for(read_teams(data_dir, args.sport, season), aliases)
    try:
        document = run_slate(
            runner, args.sport, args.date, config, results, finals, levels, directory, run_dir,
            booker, args.trigger, config_version,
        )  # fmt: skip
    except (SportyBetError, RunFailed, LineShapeError) as error:
        failed = json.loads((run_dir / "run.json").read_text("utf-8"))
        failed.update(
            {"status": "failed", "error": f"{type(error).__name__}: {error}",
             "ended_at": datetime.now(UTC).isoformat()}
        )  # fmt: skip
        _write_json(run_dir / "run.json", failed)
        raise_alert("CRITICAL", "run-failed", f"{args.sport} {args.date}: {failed['error']}")
        print(f"FAILED: {failed['error']}", file=sys.stderr)
        print(f"Partial run kept in {run_dir}", file=sys.stderr)
        return 1
    for item in document["unmatched"]:
        save_unmatched(table, args.sport, item, now)
    print(summary_text(document))
    print(f"Run folder: {run_dir}")
    return 1 if document["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
