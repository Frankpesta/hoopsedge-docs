"""The dashboard's API on v2 data (D98). Reachable only with the shared secret (D8).

Runs, tickets and the CSV are read from `data/v2/runs` (`v2.views`); the config document, aliases,
the review queue and alerts are in DynamoDB. Every change is idempotent and rate-limited (C16 to
C18), and the config document is versioned with its diff.
"""

import hmac
import json
import logging
import os
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError

from hoopsedge.api.schemas import (
    AliasRequest,
    AliasView,
    ApplySuggestion,
    Calibration,
    ConfigUpdate,
    ConfigVersion,
    ConfigView,
    Health,
    HealthDetails,
    RebookStarted,
    ResolveRequest,
    ResolveResult,
    RunDetail,
    RunRequest,
    RunStarted,
    RunSummary,
    ScheduleUpdate,
    Slate,
    Sport,
    SystemView,
    Team,
    TicketView,
    UnmatchedItem,
)
from hoopsedge.api.store import Artifacts, ConflictError, plain
from hoopsedge.common.booking_mode import is_dry_run
from hoopsedge.common.dynamo import LOCAL_ENV
from hoopsedge.v2 import golive, grading, views
from hoopsedge.v2.config_store import SCHEDULE_NAMES, ConfigDocument, document_from_stored
from hoopsedge.v2.names import normalize, seed_aliases

logger = logging.getLogger("hoopsedge.api")
EASTERN = ZoneInfo("America/New_York")
KEY_HEADER = "x-hoopsedge-key"
MUTATIONS_PER_MINUTE = 10
BUDGET_ALERTS_USD = [10, 25, 50]
SPORTS = tuple(Sport)
SAFE_ID = re.compile(r"^[\w:.\-]{1,128}$")
SAFE_ALIAS = re.compile(r"^[a-z0-9 ]{1,120}$")
ALERT_LIMIT = 50
RUN_GUARD = timedelta(minutes=10)  # A run started this recently and still running blocks Run now.
RELAY_CACHE_SECONDS = 30.0


class RunStarter(Protocol):
    def start(self, sport: str, slate: date) -> None: ...

    def rebook(self, sport: str, slate: date, ticket_id: str) -> None: ...


class ScheduleUpdater(Protocol):
    def update(self, name: str, time: str) -> None: ...


class NoSchedules:
    """The scheduler reads the document itself; a managed scheduler would plug in here."""

    def update(self, name: str, time: str) -> None:
        logger.info("Schedule %s is now %s", name, time)


def probe_relay() -> dict[str, Any]:
    """D81: SportyBet goes through the laptop relay when a proxy is set. Can we reach it?"""
    checked = datetime.now(UTC).isoformat()
    value = os.environ.get("HOOPSEDGE_SPORTYBET_PROXY", "").strip()
    if not value:
        return {"mode": "direct", "reachable": None, "checked_at": checked}
    parts = urlsplit(value)
    try:
        with socket.create_connection((parts.hostname or "", parts.port or 0), timeout=2):
            reachable = True
    except (OSError, ValueError):
        reachable = False
    return {"mode": "proxy", "reachable": reachable, "checked_at": checked}


def registry_teams(data_dir: Path, sport: str, today: date) -> list[dict[str, str]]:
    """ESPN teams for the alias picker. Polars loads only when this is asked for."""
    from hoopsedge.ingest.scores import SOURCES, read_teams, season_for  # noqa: PLC0415

    frame = read_teams(data_dir, sport, season_for(today, SOURCES[sport]))
    return [
        {"team_id": r["team_id"], "name": r["display_name"], "short_name": r["short_name"],
         "level": r["level"]}
        for r in frame.sort("display_name").iter_rows(named=True)
    ]  # fmt: skip


@dataclass
class Deps:
    store: Any
    artifacts: Artifacts
    starter: RunStarter
    schedules: ScheduleUpdater = field(default_factory=NoSchedules)
    secret: Callable[[], str | None] = lambda: os.environ.get("HOOPSEDGE_API_KEY")
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    relay: Callable[[], dict[str, Any]] = probe_relay
    teams: Callable[[str], list[dict[str, str]]] | None = None


def _not_found(what: str) -> HTTPException:
    return HTTPException(404, f"{what} not found")


def _checked_id(value: str) -> str:
    """IDs reach DynamoDB keys and file names, so anything odd is refused first."""
    if not SAFE_ID.match(value):
        raise HTTPException(422, "That ID isn't valid")
    return value


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(422, "Dates look like 2026-11-24") from error


def changes_between(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """Every value that differs, so the audit and the diff preview say exactly what changed."""
    if isinstance(old, dict) and isinstance(new, dict):
        found: list[dict[str, Any]] = []
        for key in sorted(old.keys() | new.keys()):
            found += changes_between(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        return found
    return [] if old == new else [{"path": path, "from": old, "to": new}]


def _validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else str(first["msg"])


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(
        title="HoopsEdge API", version="2", docs_url=None, redoc_url=None, openapi_url=None
    )
    store = deps.store
    data_dir = deps.artifacts.root
    relay_cache: dict[str, Any] = {}

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
        """C18: every mutating method needs a key and is rate-limited."""
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

    def config_view() -> dict[str, Any]:
        stored: dict[str, Any] | None = store.settings()
        if stored is None:
            document = document_from_stored(None)
            return {"version": 0, "document": document.model_dump(by_alias=True, mode="json")}
        document = document_from_stored(stored.get("document"))
        return {**stored, "document": document.model_dump(by_alias=True, mode="json")}

    def save_config(
        document: dict[str, Any], based_on: int, changed_by: str = "dashboard"
    ) -> ConfigView:
        current = config_view()
        if int(current["version"]) != based_on:
            raise HTTPException(409, "The config changed since you loaded it")
        try:
            checked = ConfigDocument.model_validate(document)
        except ValidationError as error:
            raise HTTPException(422, _validation_message(error)) from error
        saved = checked.model_dump(by_alias=True, mode="json")
        changes = changes_between(current["document"], saved)
        try:
            store.save_settings(saved, based_on + 1, changes, deps.now(), changed_by)
        except ConflictError as error:
            raise HTTPException(409, str(error)) from error
        for name, value in saved["schedules"].items():
            if current["document"]["schedules"].get(name) != value:
                deps.schedules.update(name, value)
        return ConfigView.model_validate(config_view())

    def team_list(sport: str) -> list[dict[str, str]]:
        source = deps.teams or (
            lambda name: registry_teams(data_dir, name, deps.now().astimezone(EASTERN).date())
        )
        return source(sport)

    @app.get("/health", response_model=Health)
    def health() -> dict[str, bool]:
        return {"ok": True}  # C17: no secret, so no data.

    api = APIRouter(dependencies=[Depends(require_key)])

    @api.get("/health/details", response_model=HealthDetails)
    def health_details() -> dict[str, Any]:
        return {
            "ok": True,
            "env": os.environ.get("HOOPSEDGE_ENV", LOCAL_ENV),
            "last_runs": views.last_run_per_sport(data_dir),
        }

    # Slates and tickets.

    @api.get("/slates/{slate_date}", response_model=Slate)
    def slate(slate_date: str, sport: Sport | None = None) -> dict[str, Any]:
        day = _parse_date(slate_date)
        chosen = [sport] if sport else list(SPORTS)
        return {
            "slate_date": day.isoformat(),
            "sports": [views.slate_view(data_dir, s.value, day, deps.now()) for s in chosen],
        }

    @api.get("/slates/{slate_date}/{sport}/csv", response_class=PlainTextResponse)
    def slate_csv(slate_date: str, sport: Sport) -> PlainTextResponse:
        """Brief §8.1: the owner's sheet, from the slate's newest full run."""
        day = _parse_date(slate_date)
        path = views.csv_file(data_dir, sport.value, day)
        if path is None:
            raise _not_found("CSV")
        return PlainTextResponse(
            path.read_text("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    @api.get("/tickets/{ticket_id}", response_model=TicketView)
    def ticket(ticket_id: str) -> dict[str, Any]:
        found = views.find_ticket(data_dir, _checked_id(ticket_id), deps.now())
        if found is None:
            raise _not_found("Ticket")
        return found

    @api.post("/tickets/{ticket_id}/rebook", response_model=RebookStarted, status_code=202)
    def rebook(ticket_id: str, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        found = views.find_ticket(data_dir, _checked_id(ticket_id), deps.now())
        if found is None:
            raise _not_found("Ticket")

        def action() -> RebookStarted:
            day = date.fromisoformat(found["slate"])
            deps.starter.rebook(found["sport"], day, ticket_id)
            return RebookStarted(
                ticket_id=ticket_id, sport=found["sport"], slate_date=found["slate"]
            )

        return once(key, f"rebook:{ticket_id}", action, 202)

    # Runs.

    @api.get("/runs", response_model=list[RunSummary])
    def runs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50, sport: Sport | None = None
    ) -> list[dict[str, Any]]:
        return views.list_runs(data_dir, limit, sport.value if sport else None)

    @api.get("/runs/{run_id}", response_model=RunDetail)
    def run(run_id: str) -> dict[str, Any]:
        found = views.run_detail(data_dir, _checked_id(run_id), deps.now())
        if found is None:
            raise _not_found("Run")
        return found

    @api.post("/runs", response_model=RunStarted, status_code=202)
    def start_run(body: RunRequest, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        now = deps.now()
        day = _parse_date(body.slate_date) if body.slate_date else now.astimezone(EASTERN).date()

        def action() -> RunStarted:
            latest = next(iter(views.list_runs(data_dir, 1, body.sport.value)), None)
            if (
                latest
                and latest["status"] == "running"
                and latest["slate"] == day.isoformat()
                and latest["started_at"]
                and now - datetime.fromisoformat(latest["started_at"]) < RUN_GUARD
            ):
                raise HTTPException(409, f"{body.sport.value} {day} already has a run going")
            deps.starter.start(body.sport.value, day)
            return RunStarted(
                sport=body.sport, slate_date=day.isoformat(), started_at=now.isoformat()
            )

        return once(key, f"run:{body.sport.value}:{day}:{now:%Y%m%d%H%M}", action, 202)

    # The review queue, teams and aliases.

    @api.get("/unmatched", response_model=list[UnmatchedItem])
    def unmatched(sport: Sport | None = None) -> list[dict[str, Any]]:
        chosen = [sport] if sport else list(SPORTS)
        return [
            {**item, "sport": s.value, "event_id": item["sk"]}
            for s in chosen
            for item in store.unmatched(s.value)
        ]

    @api.post("/unmatched/{event_id}/resolve", response_model=ResolveResult)
    def resolve_unmatched(
        event_id: str, body: ResolveRequest, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        _checked_id(event_id)

        def action() -> ResolveResult:
            for sport in SPORTS:
                item = store.unmatched_item(sport.value, event_id)
                if item is not None:
                    return _resolve(sport.value, event_id, item, body)
            raise _not_found("Unmatched event")

        return once(key, f"resolve:{event_id}", action)

    def _resolve(
        sport: str, event_id: str, item: dict[str, Any], body: ResolveRequest
    ) -> ResolveResult:
        now = deps.now()
        if body.ignore:
            store.close_unmatched(sport, event_id, "ignored", now)
            return ResolveResult(event_id=event_id, status="ignored", aliases={})
        known = {team["team_id"] for team in team_list(sport)}
        chosen = {"home": body.team_id_home, "away": body.team_id_away}
        for side, team_id in chosen.items():
            if item.get(f"{side}_team_id") is None and not team_id:
                raise HTTPException(422, f"Choose the {side} team, or ignore the event")
            if team_id and team_id not in known:
                raise HTTPException(422, f"No {sport} team {team_id}")
        home = chosen["home"] or item.get("home_team_id")
        away = chosen["away"] or item.get("away_team_id")
        if home == away:
            raise HTTPException(422, "Those are the same team")
        # Invariant 5: the owner confirmed these, so from now on the names match exactly.
        aliases = {
            normalize(str(item[f"{side}_name"])): team_id
            for side, team_id in chosen.items()
            if team_id
        }
        for alias, team_id in aliases.items():
            store.confirm_alias(sport, alias, team_id, now)
        store.close_unmatched(sport, event_id, "resolved", now)
        return ResolveResult(event_id=event_id, status="resolved", aliases=aliases)

    @api.get("/teams/{sport}", response_model=list[Team])
    def teams(sport: Sport) -> list[dict[str, str]]:
        return team_list(sport.value)

    @api.get("/aliases/{sport}", response_model=list[AliasView])
    def aliases(sport: Sport) -> list[dict[str, Any]]:
        names = {team["team_id"]: team["name"] for team in team_list(sport.value)}
        seeds = {normalize(name): team for name, team in seed_aliases(sport.value).items()}
        owner = store.owner_aliases(sport.value)
        rows = [{"alias": a, "team_id": t, "source": "seed"} for a, t in seeds.items()]
        rows += [{"alias": a, "team_id": t, "source": "owner"} for a, t in owner.items()]
        return sorted(
            ({**row, "team_name": names.get(row["team_id"])} for row in rows),
            key=lambda row: (row["source"], row["alias"]),
        )

    @api.post("/aliases", response_model=AliasView)
    def add_alias(body: AliasRequest, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        def action() -> AliasView:
            names = {team["team_id"]: team["name"] for team in team_list(body.sport.value)}
            if body.team_id not in names:
                raise HTTPException(422, f"No {body.sport.value} team {body.team_id}")
            alias = normalize(body.name)
            if not alias:
                raise HTTPException(422, "That name has no letters or digits")
            store.confirm_alias(body.sport.value, alias, body.team_id, deps.now())
            return AliasView(
                alias=alias, team_id=body.team_id, team_name=names[body.team_id], source="owner"
            )

        return once(key, f"alias:{body.sport.value}:{normalize(body.name)}", action)

    @api.delete("/aliases/{sport}/{alias}", status_code=204)
    def delete_alias(sport: Sport, alias: str, key: Annotated[str, Depends(mutation)]) -> None:
        if not SAFE_ALIAS.match(alias):
            raise HTTPException(422, "That alias isn't valid")
        if not store.delete_alias(sport.value, alias):
            raise _not_found("Owner alias")  # Seeds live in code and can't be deleted here.

    # Config (D98).

    @api.get("/config", response_model=ConfigView)
    def get_config() -> dict[str, Any]:
        return config_view()

    @api.put("/config", response_model=ConfigView)
    def put_config(body: ConfigUpdate, key: Annotated[str, Depends(mutation)]) -> JSONResponse:
        return once(key, "config", lambda: save_config(body.document, body.version))

    @api.get("/config/versions", response_model=list[ConfigVersion])
    def config_versions(
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> list[dict[str, Any]]:
        return [
            {"version": int(item["version"]), "changed_at": item.get("changed_at"),
             "changed_by": item.get("changed_by"), "diff": item.get("diff") or []}
            for item in store.config_versions(limit)
        ]  # fmt: skip

    @api.put("/schedules/{name}", response_model=ConfigView)
    def put_schedule(
        name: str, body: ScheduleUpdate, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        if name not in SCHEDULE_NAMES:
            raise _not_found("Schedule")

        def action() -> ConfigView:
            current = config_view()
            document = {
                **current["document"],
                "schedules": {**current["document"]["schedules"], name: body.time},
            }
            return save_config(document, int(current["version"]))

        return once(key, f"schedule:{name}", action)

    # Grading and calibration (brief §9, D90). Suggestions only: the owner applies them.

    @api.get("/calibration/{sport}", response_model=Calibration)
    def calibration(sport: Sport) -> dict[str, Any]:
        return grading.calibration(data_dir, sport.value, config_view()["document"])

    @api.post("/calibration/{sport}/apply", response_model=ConfigView)
    def apply_suggestion(
        sport: Sport, body: ApplySuggestion, key: Annotated[str, Depends(mutation)]
    ) -> JSONResponse:
        def action() -> ConfigView:
            current = config_view()
            if int(current["version"]) != body.version:
                raise HTTPException(409, "The config changed since you loaded it")
            found = grading.calibration(data_dir, sport.value, current["document"])
            match = next(
                (s for s in found["suggestions"]
                 if s["market"] == body.market and s["mode"] == body.mode),
                None,
            )  # fmt: skip
            if match is None or match["suggested"] is None:
                raise _not_found("Suggestion")
            thresholds = match["suggested"]["thresholds"]
            if thresholds != body.thresholds:
                raise HTTPException(409, "The suggestion changed since you loaded it")
            document = grading.apply_suggestion(
                current["document"], sport.value, body.market, body.mode, thresholds
            )
            return save_config(document, body.version, "calibration")

        return once(key, f"calibration:{sport.value}", action)

    # System.

    @api.get("/system", response_model=SystemView)
    def system() -> dict[str, Any]:
        document = config_view()["document"]
        if not relay_cache or time.monotonic() - relay_cache["at"] > RELAY_CACHE_SECONDS:
            relay_cache.update(value=deps.relay(), at=time.monotonic())

        def at(name: str) -> str | None:
            line = deps.artifacts.last_line(name)
            return None if line is None else str(line.get("at"))

        def scores(sport: str) -> str | None:
            files = list((data_dir / "ingest" / "checkpoints").glob(f"{sport}-*.json"))
            if not files:
                return None
            newest = max(path.stat().st_mtime for path in files)
            return datetime.fromtimestamp(newest, UTC).isoformat()

        last = views.last_run_per_sport(data_dir)
        now = deps.now()
        readiness = golive.readiness(
            golive.read_runs(data_dir),
            golive.read_jobs(data_dir),
            golive.alerts_since(store.table, golive.since_for(now)),
            now,
        )
        freshness: dict[str, str | None] = {"last_scheduled_job": at("scheduler/runs.jsonl")}
        for sport in SPORTS:
            freshness[f"{sport.value.lower()}_scores"] = scores(sport.value)
            freshness[f"{sport.value.lower()}_run"] = (last[sport.value] or {}).get("started_at")
        return {
            "cost": store.latest_cost(),
            "budget_usd": BUDGET_ALERTS_USD,
            "alerts": [plain(item) for item in store.alerts(ALERT_LIMIT)],
            "freshness": freshness,
            # What will really happen (D97): the switch and each container's DRY_RUN together.
            "dry_run": is_dry_run(bool(document["dry_run"])),
            "scheduled_dry_run": bool(document["dry_run"])
            or os.environ.get("HOOPSEDGE_SCHEDULER_DRY_RUN", "true").strip().lower() != "false",
            "pause_all": bool(document["pause_all"]),
            "relay": relay_cache["value"],
            "golive": readiness,
        }

    app.include_router(api)
    return app


def openapi_document(app: FastAPI) -> dict[str, Any]:
    """Generated, not served, so the routes stay hidden without the secret."""
    from fastapi.openapi.utils import get_openapi  # noqa: PLC0415

    document: dict[str, Any] = get_openapi(title=app.title, version=app.version, routes=app.routes)
    plain_document: dict[str, Any] = plain(document)
    return plain_document
