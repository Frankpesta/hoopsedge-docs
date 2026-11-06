"""The dashboard's API (docs/09). Reachable only with the shared secret (D8).

Nothing on this path may load LightGBM or polars, so it starts fast: the only exception is a
rollback, which is rare and imports the registry when it's used. D72 is enforced here, rather than
left to the dashboard: an `unproven` ticket never returns a combined probability or EV.
"""

import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Annotated, Any, Protocol
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hoopsedge.api.schemas import (
    CLOCK_PATTERN,
    UNPROVEN_NOTE,
    Evidence,
    Family,
    GameDetail,
    Health,
    HealthDetails,
    League,
    ModelsView,
    ResolveRequest,
    ResolveResult,
    RollbackRequest,
    RollbackResult,
    RunDetail,
    RunRequest,
    RunStarted,
    RunSummary,
    ScheduleUpdate,
    SettingsUpdate,
    SettingsView,
    Slate,
    SlateLeague,
    SystemView,
    Team,
    TicketCard,
    TicketDetail,
    TrackRecord,
    UnmatchedItem,
)
from hoopsedge.api.settings import (
    FAMILIES,
    SCHEDULE_NAMES,
    changes_between,
    default_settings,
    gate_verdicts,
)
from hoopsedge.api.store import Artifacts, ConflictError, plain
from hoopsedge.common.aggregates import merge_aggregates
from hoopsedge.common.booking_mode import DASHBOARD_TRIGGER, is_dry_run
from hoopsedge.common.dynamo import LOCAL_ENV
from hoopsedge.common.ids import new_ulid
from hoopsedge.teams.normalize import normalize_name

logger = logging.getLogger("hoopsedge.api")
EASTERN = ZoneInfo("America/New_York")
KEY_HEADER = "x-hoopsedge-key"
MUTATIONS_PER_MINUTE = 10
BUDGET_ALERTS_USD = [10, 25, 50]
HIDDEN_ON_UNPROVEN = ("p_raw", "p_calibrated", "p_win", "p_lose", "edge", "ev")
LEAGUES = (League.NBA, League.NCAA)
SAFE_ID = re.compile(r"^[\w:.\-]{1,128}$")
CLOCK_TIME = re.compile(CLOCK_PATTERN)
ALERT_LIMIT = 50


class RunStarter(Protocol):
    def start(self, league: str, slate: date, run_id: str) -> None: ...


class ScheduleUpdater(Protocol):
    def update(self, name: str, time: str) -> None: ...


class NoSchedules:
    """Locally the scheduler reads the settings itself. EventBridge plugs in here (Phase 1)."""

    def update(self, name: str, time: str) -> None:
        logger.info("Schedule %s is now %s", name, time)


@dataclass
class Deps:
    store: Any
    artifacts: Artifacts
    starter: RunStarter
    schedules: ScheduleUpdater = field(default_factory=NoSchedules)
    secret: Callable[[], str | None] = lambda: os.environ.get("HOOPSEDGE_API_KEY")
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def _not_found(what: str) -> HTTPException:
    return HTTPException(404, f"{what} not found")


def _checked_id(value: str) -> str:
    """IDs reach DynamoDB keys, so anything odd is refused before it gets there."""
    if not SAFE_ID.match(value):
        raise HTTPException(422, "That ID isn't valid")
    return value


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(422, "Dates look like 2026-11-06") from error


def ticket_card(item: dict[str, Any]) -> dict[str, Any]:
    """D72 lives here: an unproven ticket loses its probabilities, whatever the client asks."""
    unproven = item.get("edge_label") == "unproven"
    summaries = [dict(leg) for leg in item.get("legs_summary", [])]
    if unproven:
        for leg in summaries:
            leg["p_calibrated"] = None
            leg["edge"] = None
    return {
        **item,
        "legs": len(summaries) or int(item.get("legs", 0)),
        "legs_summary": summaries,
        "expected_value": None if unproven else item.get("ev"),
        "combined_prob": None if unproven else item.get("combined_prob"),
        "probability_note": UNPROVEN_NOTE if unproven else None,
        "correction": bool(item.get("correction")),
        "weak_legs": int(item.get("weak_legs", 0)),
    }


def leg_detail(item: dict[str, Any], unproven: bool) -> dict[str, Any]:
    leg = dict(item)
    if unproven:
        for name in HIDDEN_ON_UNPROVEN:
            leg[name] = None
    leg["correction"] = bool(leg.get("correction"))
    return leg


def _excluded(counts: dict[str, Any]) -> dict[str, int]:
    """Reason codes are upper case. The run's other counts aren't exclusions."""
    return {name: int(value) for name, value in counts.items() if name.isupper()}


def _track_record(
    days: list[dict[str, Any]], league: League | None, market: Family | None,
    first: date | None, last: date | None,
) -> TrackRecord:  # fmt: skip
    merged = merge_aggregates(days)
    settled = merged["won"] + merged["lost"]
    buckets = []
    weighted_gap = 0.0
    weighted_legs = 0
    for name, bucket in sorted(merged["buckets"].items()):
        legs = bucket["legs"]
        rate = bucket["won"] / legs if legs else None
        mean = bucket["p_sum"] / legs if legs and bucket["p_sum"] else None
        if rate is not None and mean is not None:
            weighted_gap += abs(mean - rate) * legs
            weighted_legs += legs
        buckets.append(
            {"bucket": float(name), "legs": legs, "won": bucket["won"],
             "hit_rate": rate, "mean_probability": mean}
        )  # fmt: skip
    by_day: dict[str, dict[str, Any]] = {}
    for item in days:
        day = str(item["sk"]).removeprefix("D#")
        point = by_day.setdefault(day, {"date": day, "legs": 0, "won": 0, "lost": 0})
        for name in ("legs", "won", "lost"):
            point[name] += int(item.get(name, 0))
    return TrackRecord.model_validate(
        {
            "league": league,
            "market": market,
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            **{name: merged[name] for name in ("legs", "won", "lost", "push", "void")},
            "hit_rate": merged["won"] / settled if settled else None,
            "push_rate": merged["push"] / merged["legs"] if merged["legs"] else None,
            "breakeven_hit_rate": merged["breakeven_sum"] / settled if settled else None,
            "flat_stake_profit": merged["profit"],
            "flat_stake_roi": merged["profit"] / settled if settled else None,
            "calibration_error": weighted_gap / weighted_legs if weighted_legs else None,
            "tickets": merged["tickets"],
            "missed_by": merged["missed_by"],
            "legs_won_on_lost": merged["legs_won_on_lost"],
            "legs_on_lost": merged["legs_on_lost"],
            "buckets": buckets,
            "daily": [by_day[day] for day in sorted(by_day)],
        }
    )


def _learning_curve(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for item in history:
        metrics = item.get("metrics") or {}
        points.append(
            {
                "at": str(item["sk"]).split("#", 1)[0],
                "version": item.get("version", ""),
                "decision": item.get("decision", ""),
                "log_likelihood_gain": (metrics.get("log_likelihood_gain") or {}).get("mean"),
                "crps_gain": (metrics.get("crps_gain") or {}).get("mean"),
                "games": metrics.get("games"),
                "reasons": item.get("reasons") or ([item["reason"]] if item.get("reason") else []),
            }
        )
    return points


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(
        title="HoopsEdge API", version="1", docs_url=None, redoc_url=None, openapi_url=None
    )
    store = deps.store

    @app.middleware("http")
    async def cache_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        cacheable = request.method == "GET" and response.status_code == 200
        response.headers["Cache-Control"] = "private, max-age=30" if cacheable else "no-store"
        return response

    def require_key(request: Request) -> None:
        secret = deps.secret()
        given = request.headers.get(KEY_HEADER, "")
        # With no secret configured, nothing gets in: failing open would be the worst outcome.
        if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
            host = request.client.host if request.client else "unknown"
            logger.warning("Refused a request from %s", host)
            raise HTTPException(401, "Unauthorized")

    def mutation(request: Request, idempotency_key: Annotated[str | None, Header()] = None) -> str:
        """C18: every mutating method, including PUT, needs a key and is rate-limited."""
        if not idempotency_key or not SAFE_ID.match(idempotency_key):
            raise HTTPException(400, "Mutations need an idempotency-key header")
        return idempotency_key

    def once(
        key: str, scope: str, action: Callable[[], BaseModel], status: int = 200
    ) -> JSONResponse:
        """The same key can't do a thing twice: it gets the first answer back instead."""
        earlier = store.claim_idempotency(key, scope, deps.now())
        if earlier is not None:
            if earlier.get("state") == "done":
                return JSONResponse(json.loads(earlier["body"]), int(earlier["status"]))
            raise HTTPException(409, "That request is still in progress")
        if store.count_mutation(deps.now()) > MUTATIONS_PER_MINUTE:
            store.release_idempotency(key, scope)
            raise HTTPException(429, "Too many changes in a minute")
        try:
            body = action().model_dump(mode="json")
        except Exception:
            store.release_idempotency(key, scope)  # It did nothing, so it may try again.
            raise
        store.finish_idempotency(key, scope, status, body)
        return JSONResponse(body, status)

    def settings_view() -> dict[str, Any]:
        stored: dict[str, Any] | None = store.settings()
        if stored is None:
            return {"version": 0, "document": default_settings()}
        return stored

    def apply_settings(document: dict[str, Any], based_on: int) -> SettingsView:
        current = settings_view()
        if int(current["version"]) != based_on:
            raise HTTPException(409, "Settings changed since you loaded them")
        changes = changes_between(current["document"], document)
        version = based_on + 1
        try:
            store.save_settings(document, version, changes, deps.now())
        except ConflictError as error:
            raise HTTPException(409, str(error)) from error
        return SettingsView.model_validate(store.settings())

    @app.get("/health", response_model=Health)
    def health() -> dict[str, bool]:
        return {"ok": True}  # C17: no secret, so no data.

    api = APIRouter(dependencies=[Depends(require_key)])

    @api.get("/health/details", response_model=HealthDetails)
    def health_details() -> dict[str, Any]:
        return {
            "ok": True,
            "env": os.environ.get("HOOPSEDGE_ENV", LOCAL_ENV),
            "champion_versions": {
                league.value: (store.champion(league.value) or {}).get("version")
                for league in LEAGUES
            },
            "last_runs": store.runs(5),
        }

    @api.get("/slates/{slate_date}", response_model=Slate)
    def slate(slate_date: str) -> dict[str, Any]:
        day = _parse_date(slate_date)
        leagues: list[SlateLeague] = []
        for league in LEAGUES:
            tickets, games = store.slate_items(league.value, day)
            latest = max((int(item["version"]) for item in tickets), default=0)
            current = [
                item
                for item in tickets
                if int(item["version"]) == latest and item["status"] != "superseded"
            ]
            markets: dict[str, list[TicketCard]] = {family: [] for family in FAMILIES}
            for item in sorted(current, key=lambda ticket: int(ticket["ticket_no"])):
                markets[str(item["family"])].append(TicketCard.model_validate(ticket_card(item)))
            run = store.latest_run_for_slate(league.value, day)
            counts = (run or {}).get("counts") or {}
            leagues.append(
                SlateLeague(
                    league=league,
                    run=RunSummary.model_validate(run) if run else None,
                    games=len(games),
                    first_tip=min((str(game["tip_utc"]) for game in games), default=None),
                    markets=markets,
                    excluded=_excluded(counts),
                    eligible_legs=sum(
                        len(card.legs_summary) for group in markets.values() for card in group
                    ),
                    unmatched_events=int(counts.get("unmatched_events", 0)),
                )
            )
        return {"date": day.isoformat(), "leagues": leagues}

    @api.get("/tickets/{ticket_id}", response_model=TicketDetail)
    def ticket(ticket_id: str) -> dict[str, Any]:
        item = store.ticket(_checked_id(ticket_id))
        if item is None:
            raise _not_found("Ticket")
        unproven = item.get("edge_label") == "unproven"
        versions = sorted(store.ticket_versions(item), key=lambda row: int(row["version"]))
        return {
            **item,
            "ticket": ticket_card(item),
            "leg_details": [leg_detail(leg, unproven) for leg in store.ticket_legs(ticket_id)],
            "versions": [row for row in versions if str(row.get("sk")) == "META"],
        }

    @api.get("/games/{game_id}", response_model=GameDetail)
    def game(game_id: str) -> dict[str, Any]:
        _checked_id(game_id)
        for league in LEAGUES:
            meta = store.game(league.value, game_id)
            if meta is not None:
                return _game_view(league, game_id, meta)
        raise _not_found("Game")

    def _game_view(league: League, game_id: str, meta: dict[str, Any]) -> dict[str, Any]:
        prediction = store.latest_under(league.value, game_id, "PRED#")
        detail = store.latest_under(league.value, game_id, "DETAIL#") or {}
        labels = detail.get("edge_labels") or {}
        teams = store.teams(league.value)

        def hide(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            shown = []
            for row in rows:
                entry = dict(row)
                if labels.get(str(entry.get("family"))) == "unproven":
                    for name in (*HIDDEN_ON_UNPROVEN, "p_above", "p_push"):
                        entry.pop(name, None)
                    entry["probability_note"] = UNPROVEN_NOTE
                shown.append(entry)
            return shown

        final = None
        if "home_pts" in meta and "away_pts" in meta:
            final = {"home": int(meta["home_pts"]), "away": int(meta["away_pts"])}
        injury = (prediction or {}).get("injury") or {}
        return {
            "game_id": game_id,
            "league": league,
            "game": meta,
            "home_name": teams.get(str(meta.get("home_id"))),
            "away_name": teams.get(str(meta.get("away_id"))),
            "final": final,
            "prediction": prediction,
            "key_features": (prediction or {}).get("key_features") or {},
            "injuries": injury.get("listed") or [],
            "lines": hide(detail.get("lines") or []),
            "rejections": hide(detail.get("rejections") or []),
            "legs": hide(detail.get("legs") or []),
            "edge_labels": labels,
        }

    @api.get("/runs", response_model=list[RunSummary])
    def runs(limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = store.runs(limit)
        return result

    @api.get("/runs/{run_id}", response_model=RunDetail)
    def run_detail(run_id: str) -> dict[str, Any]:
        item = store.run(_checked_id(run_id))
        if item is None:
            raise _not_found("Run")
        return {"run": item, "steps": item.get("steps") or [], "log": store.run_log(run_id)}

    @api.get("/track-record", response_model=TrackRecord)
    def track_record(
        league: League | None = None,
        market: Family | None = None,
        first: Annotated[str | None, Query(alias="from")] = None,
        last: Annotated[str | None, Query(alias="to")] = None,
    ) -> TrackRecord:
        start = _parse_date(first) if first else None
        end = _parse_date(last) if last else None
        days: list[dict[str, Any]] = []
        for one in [league] if league else LEAGUES:
            for family in [market.value] if market else FAMILIES:
                days += store.aggregates(one.value, family, start, end)
        return _track_record(days, league, market, start, end)

    @api.get("/models/{league}", response_model=ModelsView)
    def models(league: League) -> dict[str, Any]:
        history = store.model_history(league.value)
        return {
            "league": league,
            "champion": store.champion(league.value),
            "history": history,
            "learning_curve": _learning_curve(history),
            "drift": (deps.artifacts.latest_json(f"reports/learning/{league.value}") or {}).get(
                "drift"
            ),
            "gates": gate_verdicts().get(league.value, {}),
        }

    @api.get("/evidence/{league}", response_model=Evidence)
    def evidence(league: League) -> dict[str, Any]:
        champion = store.champion(league.value)
        version = (champion or {}).get("version")
        content = (
            deps.artifacts.read_json(f"models/{league.value}/{version}/evidence.json")
            if version
            else None
        )
        return {"league": league, "version": version, "evidence": content}

    @api.get("/unmatched", response_model=list[UnmatchedItem])
    def unmatched() -> list[dict[str, Any]]:
        return [
            {**item, "league": league, "event_id": item["sk"]}
            for league in LEAGUES
            for item in store.unmatched(league.value)
        ]

    @api.post("/unmatched/{event_id}/resolve", response_model=ResolveResult)
    def resolve_unmatched(
        event_id: str, body: ResolveRequest, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        _checked_id(event_id)

        def action() -> ResolveResult:
            for league in LEAGUES:
                item = store.unmatched_item(league.value, event_id)
                if item is not None:
                    return _resolve(league, event_id, item, body)
            raise _not_found("Unmatched event")

        return once(key, f"resolve:{event_id}", action)

    def _resolve(
        league: League, event_id: str, item: dict[str, Any], body: ResolveRequest
    ) -> ResolveResult:
        now = deps.now()
        if body.ignore:
            store.close_unmatched(league.value, event_id, "ignored", now)
            return ResolveResult(event_id=event_id, status="ignored", aliases={})
        if not body.team_id_home or not body.team_id_away:
            raise HTTPException(422, "Give both teams, or ignore the event")
        if body.team_id_home == body.team_id_away:
            raise HTTPException(422, "Those are the same team")
        teams = store.teams(league.value)
        for team in (body.team_id_home, body.team_id_away):
            if team not in teams:
                raise HTTPException(422, f"No {league.value} team {team}")
        # Invariant 5: from now on these names match, because the owner confirmed them.
        aliases = {
            normalize_name(str(item["home_name"])): body.team_id_home,
            normalize_name(str(item["away_name"])): body.team_id_away,
        }
        for alias, team in aliases.items():
            store.confirm_alias(league.value, alias, team, now)
        store.close_unmatched(league.value, event_id, "resolved", now)
        return ResolveResult(event_id=event_id, status="resolved", aliases=aliases)

    @api.get("/teams/{league}", response_model=list[Team])
    def teams(league: League) -> list[dict[str, str]]:
        names = store.teams(league.value)
        return [{"team_id": team, "name": names[team]} for team in sorted(names, key=names.get)]

    @api.post("/runs", response_model=RunStarted, status_code=202)
    def start_run(body: RunRequest, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        now = deps.now()
        day = _parse_date(body.slate_date) if body.slate_date else now.astimezone(EASTERN).date()

        def action() -> RunStarted:
            if store.lock_held(body.league.value, day, now):
                raise HTTPException(409, f"{body.league.value} {day} already has a run going")
            run_id = new_ulid(now)
            deps.starter.start(body.league.value, day, run_id)
            return RunStarted(run_id=run_id, league=body.league, slate_date=day.isoformat())

        return once(key, f"run:{body.league.value}:{day}", action, 202)

    @api.get("/config", response_model=SettingsView)
    def get_config() -> dict[str, Any]:
        return settings_view()

    @api.put("/config", response_model=SettingsView)
    def put_config(body: SettingsUpdate, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        def action() -> SettingsView:
            document = body.document.model_dump(mode="json")
            bad = set(document["schedules"]) ^ set(SCHEDULE_NAMES)
            if bad:
                raise HTTPException(422, f"Schedules must be exactly {list(SCHEDULE_NAMES)}")
            for value in document["schedules"].values():
                if not CLOCK_TIME.match(value):
                    raise HTTPException(422, f"{value} isn't a time like 22:00")
            # Saved first: a refused change must not have moved any schedule.
            before = settings_view()["document"]["schedules"]
            result = apply_settings(document, body.version)
            for name, value in document["schedules"].items():
                if before.get(name) != value:
                    deps.schedules.update(name, value)
            return result

        return once(key, "config", action)

    @api.put("/schedules/{name}", response_model=SettingsView)
    def put_schedule(
        name: str, body: ScheduleUpdate, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        if name not in SCHEDULE_NAMES:
            raise _not_found("Schedule")

        def action() -> SettingsView:
            current = settings_view()
            document = {
                **current["document"],
                "schedules": {**current["document"]["schedules"], name: body.time},
            }
            result = apply_settings(document, int(current["version"]))
            deps.schedules.update(name, body.time)
            return result

        return once(key, f"schedule:{name}", action)

    @api.post("/models/{league}/rollback", response_model=RollbackResult)
    def rollback(
        league: League, body: RollbackRequest, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        def action() -> RollbackResult:
            # Rare, and the one path that needs the model code, so it's imported only when used.
            from hoopsedge.learning.registry import ModelRegistry, RegistryError  # noqa: PLC0415

            registry = ModelRegistry(store.table, deps.artifacts.root / "models")
            try:
                champion = registry.rollback(league.value, body.version, deps.now(), body.reason)
            except RegistryError as error:
                raise HTTPException(422, str(error)) from error
            return RollbackResult(league=league, version=champion.version, reason=champion.reason)

        return once(key, f"rollback:{league.value}:{body.version}", action)

    @api.get("/system", response_model=SystemView)
    def system() -> dict[str, Any]:
        document = settings_view()["document"]

        def at(name: str) -> str | None:
            line = deps.artifacts.last_line(name)
            return None if line is None else str(line.get("at"))

        return {
            "cost": store.latest_cost(),
            "budget_usd": BUDGET_ALERTS_USD,
            "alerts": store.alerts(ALERT_LIMIT),
            "freshness": {
                "last_ingest": at("ingest/runs.jsonl"),
                "last_odds_fetch": at("collector/runs.jsonl"),
                "last_scheduled_job": at("scheduler/runs.jsonl"),
            },
            # What will really happen (D78), since the environment can overrule the switch.
            "dry_run": is_dry_run(bool(document["dry_run"]), DASHBOARD_TRIGGER),
            "scheduled_dry_run": is_dry_run(bool(document["dry_run"]), "scheduled"),
            "pause_all": bool(document["pause_all"]),
        }

    app.include_router(api)
    return app


def openapi_document(app: FastAPI) -> dict[str, Any]:
    """Generated, not served, so the routes stay hidden without the secret."""
    from fastapi.openapi.utils import get_openapi  # noqa: PLC0415

    document: dict[str, Any] = get_openapi(title=app.title, version=app.version, routes=app.routes)
    plain_document: dict[str, Any] = plain(document)
    return plain_document
