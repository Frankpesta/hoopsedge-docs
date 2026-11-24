"""Every v2 response and request, typed, so the dashboard's TypeScript types can't drift (D98)."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CLOCK_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
IMPLIED_NOTE = (
    "Bookmaker-implied: the product of 1/odds for every leg, margin included. Each leg is close "
    "to a coin flip at main-line odds, so long tickets very rarely land (brief §13)."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Sport(StrEnum):
    NBA = "NBA"
    NCAAB = "NCAAB"
    NCAAF = "NCAAF"


class Health(_Model):
    ok: bool


class HealthDetails(_Model):
    ok: bool
    env: str
    last_runs: dict[str, "RunSummary | None"]


# Slates.


class L5(_Model):
    pf: float
    pa: float
    n: int


class TeamPickView(_Model):
    team: str
    side: str
    direction: str
    line: float
    odds: float | None
    projection: float
    edge: float


class SlateRow(_Model):
    event_id: str
    game_id: str
    home: str
    away: str
    kickoff: str
    started: bool
    eligible: bool
    skip_reason: str | None
    home_tiers: list[str]
    away_tiers: list[str]
    home_l5: L5
    away_l5: L5
    sum_of_avgs: float
    model_total: float
    outlook: str
    main_line: float | None
    lean: str | None
    edge_main: float | None
    booked_line: float | None
    booked_odds: float | None
    edge_booked: float | None
    home_proj: float
    away_proj: float
    team_pick: TeamPickView | None
    flags: list[str]
    same_level_total: float | None
    same_level_lean: str | None


class LegView(_Model):
    kind: str
    event_id: str
    home: str
    away: str
    kickoff: str
    started: bool
    mode: str
    team: str | None
    direction: str
    projection: float
    booked_line: float
    main_line: float
    odds: float
    main_odds: float
    edge_main: float
    edge_booked: float
    flags: list[str]


class TicketView(_Model):
    ticket_id: str
    sport: Sport
    slate: str
    run_id: str
    kind: str
    number: int
    status: str
    code: str | None
    reason: str | None
    dry_run: bool
    total_odds: float
    bookmaker_implied_probability: float
    implied_note: str = IMPLIED_NOTE
    earliest_kickoff: str
    next_kickoff: str | None
    started_legs: int
    model_disagrees: int
    superseded_by: str | None
    rebooked_by: str | None
    rebook_of: str | None
    current: bool
    legs: list[LegView]


class RunSummary(_Model):
    run_id: str
    sport: Sport
    slate: str
    trigger: str
    status: str
    dry_run: bool
    error: str | None
    started_at: str | None
    ended_at: str | None
    config_version: int | None
    counts: dict[str, int]
    tickets: int


class SlateSport(_Model):
    sport: Sport
    run: RunSummary | None
    rows: list[SlateRow]
    tickets: list[TicketView]
    unmatched: int
    csv: bool


class Slate(_Model):
    slate_date: str
    sports: list[SlateSport]


class RunDetail(RunSummary):
    tickets_detail: list[TicketView]
    unmatched_events: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    dropped: dict[str, int]


class RunRequest(_Model):
    sport: Sport
    slate_date: str | None = None


class RunStarted(_Model):
    sport: Sport
    slate_date: str
    started_at: str


class RebookStarted(_Model):
    ticket_id: str
    sport: Sport
    slate_date: str


# The review queue and aliases.


class UnmatchedItem(_Model):
    sport: Sport
    event_id: str
    home_name: str
    away_name: str
    start: str
    reason: str | None
    home_team_id: str | None = None
    away_team_id: str | None = None
    home_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    away_suggestions: list[dict[str, Any]] = Field(default_factory=list)


class ResolveRequest(_Model):
    """A team for each side that wasn't matched, or `ignore` (a Division II game, say)."""

    team_id_home: str | None = None
    team_id_away: str | None = None
    ignore: bool = False


class ResolveResult(_Model):
    event_id: str
    status: str
    aliases: dict[str, str]


class Team(_Model):
    team_id: str
    name: str
    short_name: str
    level: str


class AliasView(_Model):
    alias: str
    team_id: str
    team_name: str | None
    source: str  # seed or owner


class AliasRequest(_Model):
    sport: Sport
    name: str = Field(min_length=2, max_length=120)
    team_id: str


# Config.


class ConfigView(_Model):
    version: int
    document: dict[str, Any]  # The full v2 config document; validated on save (D98).
    changed_at: str | None = None
    changed_by: str | None = None
    diff: list[dict[str, Any]] = Field(default_factory=list)


class ConfigUpdate(_Model):
    version: int  # The version these changes were based on.
    document: dict[str, Any]


class ConfigVersion(_Model):
    version: int
    changed_at: str | None
    changed_by: str | None
    diff: list[dict[str, Any]]


class ScheduleUpdate(_Model):
    time: str = Field(pattern=CLOCK_PATTERN)


# System.


class RelayView(_Model):
    mode: str  # direct or proxy
    reachable: bool | None  # None when there is no proxy to check.
    checked_at: str


class SystemView(_Model):
    cost: dict[str, Any] | None
    budget_usd: list[int]
    alerts: list[dict[str, Any]]
    freshness: dict[str, str | None]
    dry_run: bool  # Whether Run now makes practice codes.
    scheduled_dry_run: bool
    pause_all: bool
    relay: RelayView


HealthDetails.model_rebuild()
