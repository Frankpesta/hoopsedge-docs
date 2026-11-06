"""The slate pipeline for one league and date (docs/00 section 4, invariants 2, 3, 4, 6, 7, 8).

    docker compose run --rm pipeline python -m hoopsedge.pipeline.slate run --league NBA

Each step is a function, so AWS can run them as Step Functions states later. Locally it always
runs dry: `booking.is_dry_run` refuses anything else, so nothing here can create a real code.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hoopsedge.common.dynamo import GameTable, connect, ensure_table, table_name
from hoopsedge.common.http import PoliteClient
from hoopsedge.common.ids import new_ulid
from hoopsedge.engine import (
    BuildResult,
    EngineConfig,
    Offer,
    Prediction,
    build_tickets,
    load_engine_config,
    score_offers,
)
from hoopsedge.engine.config import apply_settings, with_calibrator_sources
from hoopsedge.engine.recheck import (
    REJECTED_AT_BOOKING,
    compare_builds,
    rebuild_ticket,
    still_bookable,
)
from hoopsedge.engine.schemas import Ticket
from hoopsedge.grading.data import team_names
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.ingest.live import USER_AGENT, Live, ingest_once
from hoopsedge.learning.data import write_log
from hoopsedge.learning.registry import ModelRegistry
from hoopsedge.pipeline.predict import game_ref, predict_games
from hoopsedge.pipeline.store import (
    CRITICAL,
    DRY_RUN,
    FAILED,
    INVALID,
    PENDING_BOOKING,
    VERIFIED,
    WARNING,
    PipelineStore,
)
from hoopsedge.sportybet.booking import create_code, dry_run_code, is_dry_run
from hoopsedge.sportybet.client import (
    BlockedError,
    BusinessError,
    SportyBetClient,
    SportyBetError,
    build_client,
)
from hoopsedge.sportybet.offers import OfferSnapshot, fetch_snapshot, normalize_event, slate_bounds
from hoopsedge.sportybet.verification import verify_code
from hoopsedge.teams.matcher import CanonicalGame, TeamIndex, confirmed_aliases, match_event

LAGOS = ZoneInfo("Africa/Lagos")
MAX_UNMATCHED_BEFORE_WARNING = 5
DEFAULT_DATA_DIR = "/workspace/data"

Predictor = Callable[[str, Sequence[dict[str, Any]], datetime], list[Prediction]]


class SlateLocked(Exception):
    """Another run holds this league and slate."""


@dataclass
class SlateContext:
    data_dir: Path
    store: PipelineStore
    client: SportyBetClient
    config: EngineConfig
    predict: Predictor
    refresh: Callable[[date], Any]  # Finals and injuries. Its own failures are logged, not fatal.
    dry_run: bool = True
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    aliases: dict[str, TeamIndex] = field(default_factory=dict)
    team_names: dict[str, str] = field(default_factory=dict)

    def game_names(self, games: Sequence[CanonicalGame]) -> dict[str, tuple[str, str]]:
        """Each game's home and away names. A team with no name shows its ID, never a guess."""
        return {
            game.game_id: (
                self.team_names.get(game.home_id, game.home_id),
                self.team_names.get(game.away_id, game.away_id),
            )
            for game in games
        }

    def team_index(self, league: str) -> TeamIndex:
        if league not in self.aliases:
            confirmed = confirmed_aliases(self.store.confirmed_aliases(league))
            self.aliases[league] = TeamIndex.load(self.data_dir, league, confirmed)
        return self.aliases[league]


@dataclass
class RunSummary:
    run_id: str
    league: str
    slate: date
    status: str = "running"
    error: str | None = None
    tickets: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)


def _canonical(item: dict[str, Any]) -> CanonicalGame | None:
    try:
        neutral = item.get("neutral")
        return CanonicalGame(
            game_id=str(item["game_id"]),
            home_id=str(item["home_id"]),
            away_id=str(item["away_id"]),
            tip_utc=datetime.fromisoformat(str(item["tip_utc"])),
            neutral=None if neutral is None else bool(neutral),
            slate_date_et=date.fromisoformat(str(item["slate_date_et"])),
        )
    except (KeyError, ValueError):
        return None


@dataclass(frozen=True)
class _Matched:
    offers: list[Offer]
    game_ids: set[str]
    unmatched: int


def match_snapshot(
    ctx: SlateContext, summary: RunSummary, snapshot: OfferSnapshot, games: Sequence[CanonicalGame]
) -> _Matched:
    """Offers for this slate's games. A team we can't place goes to the queue, never a guess."""
    index = ctx.team_index(summary.league)
    first, last = slate_bounds(summary.slate)
    offers: list[Offer] = []
    game_ids: set[str] = set()
    unmatched = 0
    by_id = {game.game_id: game for game in games}
    for event in snapshot.events:
        if not first <= event.start <= last:
            continue
        match = match_event(
            event.raw.homeTeamName, event.raw.awayTeamName, event.start, games, index
        )
        if match.game_id is not None:
            if by_id[match.game_id].slate_date_et != summary.slate:
                continue  # Within the window, but really a neighbouring slate's game.
            game_ids.add(match.game_id)
            offers += normalize_event(event, match.game_id, match.orientation, snapshot.fetched_at)
        elif event.start.astimezone(EASTERN).date() == summary.slate:
            unmatched += 1
            ctx.store.save_unmatched(
                summary.league,
                event.event_id,
                event.raw.homeTeamName,
                event.raw.awayTeamName,
                event.start,
                match,
            )
            offers += normalize_event(event, None, None, snapshot.fetched_at)
    return _Matched(offers, game_ids, unmatched)


def _offer_index(offers: Sequence[Offer]) -> dict[tuple[str, str, str], Offer]:
    return {(offer.event_id, offer.market_id, offer.specifier): offer for offer in offers}


@dataclass
class _Booking:
    ctx: SlateContext
    summary: RunSummary
    games: Sequence[CanonicalGame]
    version: int

    def _log(self, step: str, event: str, payload: Any = None) -> None:
        self.ctx.store.log(self.summary.run_id, self.ctx.now(), step, event, payload)

    def _alert(self, key: str, message: str) -> None:
        self.ctx.store.alert(self.ctx.now(), CRITICAL, key, message, self.summary.run_id)

    def _finish(
        self,
        ticket_id: str,
        ticket: Ticket,
        status: str,
        code: str | None = None,
        reason: str | None = None,
        verification: dict[str, Any] | None = None,
    ) -> None:
        self.ctx.store.save_ticket(
            ticket_id,
            ticket,
            self.summary.run_id,
            self.summary.slate,
            self.version,
            status,
            code,
            reason,
            verification,
            names=self.ctx.game_names(self.games),
        )
        self.summary.tickets.append(
            {
                "ticket_id": ticket_id,
                "family": ticket.family,
                "ticket_no": ticket.ticket_no,
                "legs": len(ticket.legs),
                "status": status,
                "code": code,
                "reason": reason,
                "combined_odds": round(ticket.combined_odds, 2),
                "combined_prob": ticket.combined_prob,
                "expected_value": round(ticket.expected_value, 4),
                "passed_gate": ticket.passed_gate,
                "edge_label": ticket.edge_label,
            }
        )
        if status in {INVALID, FAILED}:
            self._alert(f"ticket-{status}", f"{ticket.family} {ticket.ticket_no}: {reason}")

    def _retry_after_rejection(self, ticket: Ticket, error: BusinessError) -> tuple[Ticket, str]:
        """D35: re-fetch, drop what's no longer bookable, and try exactly once more."""
        self._log("Book", "BOOKING_REJECTED", {"code": error.code, "message": error.message})
        now = self.ctx.now()
        fresh = fetch_snapshot(self.ctx.client, self.summary.league, now)
        matched = match_snapshot(self.ctx, self.summary, fresh, self.games)
        market = self.ctx.config.markets[ticket.league][ticket.family]
        assert market.min_edge is not None  # noqa: S101 - The ticket passed it to exist.
        earliest = now + timedelta(minutes=self.ctx.config.min_minutes_before_tip)
        kept, dropped = still_bookable(
            ticket, _offer_index(matched.offers), market.min_edge, earliest
        )
        for leg in dropped:
            self._log(
                "Book", REJECTED_AT_BOOKING, {"game_id": leg.game_id, "specifier": leg.specifier}
            )
        if len(kept) < self.ctx.config.min_legs:
            raise SportyBetError(
                f"Rejected, and only {len(kept)} legs remain of {len(ticket.legs)}"
            )
        reduced = rebuild_ticket(ticket, kept)
        return reduced, create_code(self.ctx.client, reduced.legs)

    def book(self, ticket: Ticket) -> None:
        ctx = self.ctx
        ticket_id = new_ulid(ctx.now())
        ctx.store.save_ticket(
            ticket_id, ticket, self.summary.run_id, self.summary.slate, self.version,
            PENDING_BOOKING, names=ctx.game_names(self.games),
        )  # fmt: skip
        if ctx.dry_run:
            self._finish(ticket_id, ticket, DRY_RUN, dry_run_code(ticket_id))
            return
        # C10: a long run can outlive its odds, and invariant 3 applies to each booking.
        limit = timedelta(seconds=ctx.config.max_offer_age_seconds)
        if any(ctx.now() - leg.offer_fetched_at > limit for leg in ticket.legs):
            self._finish(ticket_id, ticket, FAILED, reason="STALE_ODDS at booking")
            return
        reused = ctx.store.verified_code(ticket.selection_hash)
        if reused is not None:
            self._log("Book", "CODE_REUSED", {"code": reused})
            self._finish(ticket_id, ticket, VERIFIED, reused, "Reused a verified code")
            return
        try:
            try:
                code = create_code(ctx.client, ticket.legs)
            except BusinessError as error:
                ticket, code = self._retry_after_rejection(ticket, error)
        except BlockedError:
            raise  # Bot protection stops the whole run.
        except SportyBetError as error:
            self._finish(ticket_id, ticket, FAILED, reason=f"Booking failed: {error}")
            return
        self.verify(ticket_id, ticket, code)

    def verify(self, ticket_id: str, ticket: Ticket, code: str) -> None:
        try:
            document = self.ctx.client.lookup(code)
        except BlockedError:
            raise
        except SportyBetError as error:
            self._finish(ticket_id, ticket, INVALID, code, f"Lookup failed: {error}")
            return
        result = verify_code(ticket.legs, document)
        details = {
            "missing": [list(item) for item in result.missing],
            "extra": [list(item) for item in result.extra],
            "odds_differences": [list(item) for item in result.odds_differences],
        }
        if result.verified:
            self.ctx.store.remember_code(ticket.selection_hash, code, ticket_id)
            self._finish(ticket_id, ticket, VERIFIED, code, verification=details)
        else:
            self._finish(ticket_id, ticket, INVALID, code, result.reason, details)


def _step(summary: RunSummary, name: str, started: datetime, ended: datetime, **extra: Any) -> None:
    summary.steps.append(
        {"name": name, "started": started.isoformat(), "ended": ended.isoformat(), **extra}
    )


def _counts(result: BuildResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in result.rejections:
        counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
    return counts


def _write_artifact(ctx: SlateContext, summary: RunSummary, extra: dict[str, Any]) -> None:
    """Kept even for a failed run (invariant 7). S3 takes this over with Phase 1."""
    folder = ctx.data_dir / "runs" / summary.run_id
    folder.mkdir(parents=True, exist_ok=True)
    document = {
        "run_id": summary.run_id,
        "league": summary.league,
        "slate": summary.slate.isoformat(),
        "status": summary.status,
        "error": summary.error,
        "tickets": summary.tickets,
        "counts": summary.counts,
        "steps": summary.steps,
        **extra,
    }
    (folder / "result.json").write_text(json.dumps(document, indent=1, default=str), "utf-8")


def _run_steps(ctx: SlateContext, summary: RunSummary, artifact: dict[str, Any]) -> None:
    league, slate = summary.league, summary.slate
    run_id = summary.run_id

    started = ctx.now()
    refreshed = ctx.refresh(slate)
    failures = getattr(refreshed, "failures", {})
    if failures:
        ctx.store.log(run_id, ctx.now(), "RefreshIngest", "FAILURES", failures)
    _step(summary, "RefreshIngest", started, ctx.now(), failures=len(failures))

    # Neighbouring slates too, so a game near midnight isn't sent to the queue by mistake.
    items = [
        item
        for day in (slate - timedelta(days=1), slate, slate + timedelta(days=1))
        for item in ctx.store.slate_games(league, day)
    ]
    games = [game for game in map(_canonical, items) if game is not None]

    started = ctx.now()
    snapshot = fetch_snapshot(ctx.client, league, started)
    if snapshot.failures:
        ctx.store.alert(
            ctx.now(), WARNING, "offer-shape", f"{len(snapshot.failures)} events skipped", run_id
        )
        ctx.store.log(run_id, ctx.now(), "FetchOffers", "EVENTS_SKIPPED", snapshot.failures)
    _step(summary, "FetchOffers", started, ctx.now(), events=len(snapshot.events))

    started = ctx.now()
    matched = match_snapshot(ctx, summary, snapshot, games)
    summary.counts["matched_games"] = len(matched.game_ids)
    summary.counts["unmatched_events"] = matched.unmatched
    if matched.unmatched > MAX_UNMATCHED_BEFORE_WARNING:
        ctx.store.alert(
            ctx.now(), WARNING, "unmatched", f"{matched.unmatched} unmatched events", run_id
        )
    _step(summary, "MatchTeams", started, ctx.now())

    started = ctx.now()
    slate_items = [item for item in items if str(item.get("game_id")) in matched.game_ids]
    predictions = ctx.predict(league, slate_items, started)
    for prediction in predictions:
        ctx.store.save_prediction(run_id, prediction)
    summary.counts["predictions"] = len(predictions)
    artifact["predictions"] = [item.model_dump(mode="json") for item in predictions]
    _step(summary, "Predict", started, ctx.now())

    started = ctx.now()
    first_build = build_tickets(predictions, matched.offers, ctx.config, started)
    artifact["first_build"] = first_build.model_dump(mode="json")
    _step(summary, "BuildTickets", started, ctx.now(), tickets=len(first_build.tickets))

    # RecheckOffers: invariant 3. The rebuild re-scores anything that moved, by the same rules.
    started = ctx.now()
    fresh = fetch_snapshot(ctx.client, league, started)
    rematched = match_snapshot(ctx, summary, fresh, games)
    final = build_tickets(predictions, rematched.offers, ctx.config, ctx.now())
    changes = compare_builds(first_build, final)
    for change in changes:
        ctx.store.log(run_id, ctx.now(), "RecheckOffers", change.kind, vars(change))
    artifact["final_build"] = final.model_dump(mode="json")
    summary.counts.update(_counts(final))
    # Everything offered, not only the picks, is what real-line calibrators learn from (D72).
    write_log(
        ctx.data_dir,
        league,
        slate,
        run_id,
        predictions,
        score_offers(predictions, rematched.offers),
    )
    _save_game_details(ctx, run_id, league, predictions, rematched.offers, final)
    _step(summary, "RecheckOffers", started, ctx.now(), changes=len(changes))

    started = ctx.now()
    version = ctx.store.next_version(league, slate)
    booking = _Booking(ctx, summary, games, version)
    for ticket in final.tickets:
        booking.book(ticket)
    _step(summary, "Book", started, ctx.now(), tickets=len(final.tickets))

    superseded = ctx.store.supersede(league, slate, version)
    _step(summary, "Finalize", ctx.now(), ctx.now(), superseded=superseded)


def _save_game_details(
    ctx: SlateContext,
    run_id: str,
    league: str,
    predictions: Sequence[Prediction],
    offers: Sequence[Offer],
    final: BuildResult,
) -> None:
    lines = score_offers(predictions, offers)
    labels = {
        family: settings.edge_label for family, settings in ctx.config.markets[league].items()
    }
    legs = [leg for ticket in final.tickets for leg in ticket.legs]
    for prediction in predictions:
        game = prediction.game_id
        ctx.store.save_game_detail(
            run_id,
            league,
            game,
            [item for item in lines if item.game_id == game],
            [item for item in final.rejections if item.game_id == game],
            [item for item in legs if item.game_id == game],
            labels,
        )


def run_slate(
    ctx: SlateContext,
    league: str,
    slate: date,
    trigger: str = "manual",
    run_id: str | None = None,
) -> RunSummary:
    now = ctx.now()
    summary = RunSummary(run_id or new_ulid(now), league, slate)
    run_id = summary.run_id
    if not ctx.store.acquire_lock(league, slate, run_id, now):
        raise SlateLocked(f"{league} {slate} already has a run in progress")
    fields = {
        "league": league,
        "slate_date": slate.isoformat(),
        "trigger": trigger,
        "dry_run": ctx.dry_run,
        "started_at": now.isoformat(),
    }
    ctx.store.save_run(run_id, {**fields, "status": "running"})
    artifact: dict[str, Any] = {}
    try:
        _run_steps(ctx, summary, artifact)
        bad = [item for item in summary.tickets if item["status"] in {INVALID, FAILED}]
        summary.status = "failed" if bad else "succeeded"
        if bad:
            summary.error = f"{len(bad)} tickets invalid or failed"
    except Exception as error:  # noqa: BLE001 - Whatever broke, the run must end marked failed.
        summary.status = "failed"
        summary.error = f"{type(error).__name__}: {error}"
        ctx.store.alert(ctx.now(), CRITICAL, "run-failed", summary.error, run_id)
    finally:
        ctx.store.release_lock(league, slate, run_id)
        _write_artifact(ctx, summary, artifact)
    ctx.store.save_run(
        run_id,
        {
            **fields,
            "status": summary.status,
            "error": summary.error,
            "steps": summary.steps,
            "counts": summary.counts,
            "ended_at": ctx.now().isoformat(),
        },
    )
    return summary


def _build_context(league: str, data_dir: Path, run_folder: Path) -> SlateContext:
    resource = connect()
    ensure_table(resource, table_name())
    table = resource.Table(table_name())
    store = PipelineStore(table)
    registry = ModelRegistry(table, data_dir / "models")
    champion = registry.require(league)
    model_folder = registry.model_folder(league, champion.version)
    settings = store.settings()
    config = with_calibrator_sources(
        apply_settings(
            load_engine_config({league: registry.calibrators(league, champion)}), settings
        ),
        league,
        champion.calibrator_sources,
    )
    names = team_names(data_dir)
    league_names = {team: name for (owner, team), name in names.items() if owner == league}
    store.sync_teams(league, league_names)

    def predict(chosen: str, items: Sequence[dict[str, Any]], now: datetime) -> list[Prediction]:
        refs = [game_ref(chosen, item) for item in items]
        return predict_games(
            data_dir, chosen, [ref for ref in refs if ref is not None], now, model_folder
        )

    def refresh(slate: date) -> Any:
        live = Live(data_dir, PoliteClient(USER_AGENT, rate_per_second=2.0), GameTable(table))
        return ingest_once(live, [slate - timedelta(days=1), slate, slate + timedelta(days=1)])

    return SlateContext(
        data_dir=data_dir,
        store=store,
        client=build_client(run_folder),
        config=config,
        predict=predict,
        refresh=refresh,
        dry_run=is_dry_run(bool(settings and settings.get("dry_run"))),
        team_names=league_names,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--league", choices=["NBA", "NCAA"], required=True)
    run.add_argument("--date", help="Slate date, US Eastern. Defaults to today.")
    run.add_argument("--run-id", help="Set by the API, so Run now can return it straight away.")
    run.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "dashboard"])
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    slate = (
        date.fromisoformat(args.date) if args.date else datetime.now(UTC).astimezone(EASTERN).date()
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    context = _build_context(args.league, data_dir, data_dir / f"raw/sportybet/run-{stamp}")
    summary = run_slate(context, args.league, slate, args.trigger, args.run_id)
    print(
        json.dumps(
            {
                "run_id": summary.run_id,
                "status": summary.status,
                "error": summary.error,
                "counts": summary.counts,
                "tickets": summary.tickets,
                "dry_run": context.dry_run,
            },
            indent=1,
            default=str,
        )
    )
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
