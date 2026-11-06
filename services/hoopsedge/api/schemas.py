"""Every response and request, typed, so the dashboard's TypeScript types can't drift (docs/09)."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CLOCK_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
UNPROVEN_NOTE = (
    "Unproven: this market's probabilities haven't been tested at real bookmaker lines, and when "
    "they were tested on full-game totals, confident picks won about half the time. Treat each "
    "leg as about 50%."
)


class League(StrEnum):
    NBA = "NBA"
    NCAA = "NCAA"


class Family(StrEnum):
    HANDICAP = "handicap"
    TOTAL_FT = "total_ft"
    TOTAL_1H = "total_1h"
    TEAM_TOTAL = "team_total"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Health(_Model):
    ok: bool


class HealthDetails(_Model):
    ok: bool
    env: str
    champion_versions: dict[str, str | None]
    last_runs: list["RunSummary"]


class LegSummary(_Model):
    rank: int
    game_id: str
    matchup: str
    pick: str
    odds: float
    p_calibrated: float | None  # Null on an unproven market (D72).
    fair: float
    edge: float | None
    tip_time: str


class TicketCard(_Model):
    ticket_id: str
    league: League
    family: Family
    ticket_no: int
    version: int
    status: str
    settle_status: str
    booking_code: str | None
    reason: str | None = None
    legs: int
    combined_odds: float
    combined_prob: float | None  # Null on an unproven market (D72).
    expected_value: float | None
    passed_gate: bool  # False is a market in force that failed its gate (D65).
    edge_label: str
    probability_note: str | None
    correction: bool = False
    legs_summary: list[LegSummary] = Field(default_factory=list)


class SlateLeague(_Model):
    league: League
    run: "RunSummary | None"
    games: int
    first_tip: str | None
    markets: dict[str, list[TicketCard]]
    excluded: dict[str, int]
    eligible_legs: int
    unmatched_events: int


class Slate(_Model):
    date: str
    leagues: list[SlateLeague]


class LegDetail(_Model):
    rank: int
    game_id: str
    event_id: str
    matchup: str | None = None
    pick: str | None = None
    family: str
    specifier: str
    line: float
    side: str
    team: str | None = None
    outcome_id: str
    odds: float
    fair: float
    tip_time: str
    status: str
    p_raw: float | None = None
    p_calibrated: float | None = None
    p_win: float | None = None
    p_push: float | None = None
    p_lose: float | None = None
    edge: float | None = None
    ev: float | None = None
    actual_value: float | None = None
    model_error: float | None = None
    predicted_mean: float | None = None
    correction: bool = False


class TicketVersion(_Model):
    ticket_id: str
    version: int
    status: str
    booking_code: str | None
    run_id: str


class TicketDetail(_Model):
    ticket: TicketCard
    run_id: str
    slate_date: str
    selection_hash: str
    settled_combined_odds: float | None = None
    verification: dict[str, Any] | None = None
    leg_details: list[LegDetail]
    versions: list[TicketVersion]


class GameDetail(_Model):
    game_id: str
    league: League
    game: dict[str, Any]
    home_name: str | None
    away_name: str | None
    final: dict[str, int] | None
    prediction: dict[str, Any] | None
    key_features: dict[str, float | None]
    injuries: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    legs: list[dict[str, Any]]
    edge_labels: dict[str, str]


class RunSummary(_Model):
    run_id: str
    league: str
    slate_date: str
    status: str
    trigger: str | None = None
    dry_run: bool | None = None
    started_at: str
    ended_at: str | None = None
    error: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


class RunDetail(_Model):
    run: RunSummary
    steps: list[dict[str, Any]]
    log: list[dict[str, Any]]


class RunStarted(_Model):
    run_id: str
    league: League
    slate_date: str


class Bucket(_Model):
    bucket: float
    legs: int
    won: int
    hit_rate: float | None
    mean_probability: float | None


class DailyPoint(_Model):
    date: str
    legs: int
    won: int
    lost: int


class TrackRecord(_Model):
    league: League | None
    market: Family | None
    first: str | None
    last: str | None
    legs: int
    won: int
    lost: int
    push: int
    void: int
    hit_rate: float | None
    push_rate: float | None
    breakeven_hit_rate: float | None
    flat_stake_profit: float
    flat_stake_roi: float | None
    calibration_error: float | None
    tickets: dict[str, int]
    missed_by: dict[str, int]
    legs_won_on_lost: int
    legs_on_lost: int
    buckets: list[Bucket]
    daily: list[DailyPoint]


class LearningPoint(_Model):
    at: str
    version: str
    decision: str
    log_likelihood_gain: float | None
    crps_gain: float | None
    games: int | None
    reasons: list[str]


class ModelsView(_Model):
    league: League
    champion: dict[str, Any] | None
    history: list[dict[str, Any]]
    learning_curve: list[LearningPoint]
    drift: dict[str, Any] | None
    gates: dict[str, Any]


class Evidence(_Model):
    league: League
    version: str | None
    evidence: dict[str, Any] | None


class UnmatchedItem(_Model):
    league: League
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


class MarketSetting(_Model):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    min_prob: float | None = Field(default=None, ge=0.0, le=1.0)
    min_edge: float | None = Field(default=None, ge=-1.0, le=1.0)


class LeagueSetting(_Model):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class SettingsDocument(_Model):
    model_config = ConfigDict(extra="forbid")

    schedules: dict[str, str]
    leagues: dict[League, LeagueSetting]
    markets: dict[League, dict[Family, MarketSetting]]
    min_legs: int = Field(ge=2)  # D4: a ticket needs at least two legs.
    min_minutes_before_tip: int = Field(ge=20)  # Invariant 2 can be tightened, never loosened.
    allow_alt_lines: bool
    sportybet_proxy_enabled: bool
    dry_run: bool
    pause_all: bool


class SettingsView(_Model):
    version: int
    document: SettingsDocument
    changed_at: str | None = None
    changed_by: str | None = None
    diff: list[dict[str, Any]] = Field(default_factory=list)


class SettingsUpdate(_Model):
    version: int  # The version these changes were based on.
    document: SettingsDocument


class ScheduleUpdate(_Model):
    time: str = Field(pattern=CLOCK_PATTERN)


class RunRequest(_Model):
    league: League
    slate_date: str | None = None


class RollbackRequest(_Model):
    version: str
    reason: str = "Rolled back from the dashboard"


class RollbackResult(_Model):
    league: League
    version: str
    reason: str


class SystemView(_Model):
    cost: dict[str, Any] | None
    budget_usd: list[int]
    alerts: list[dict[str, Any]]
    freshness: dict[str, str | None]
    dry_run: bool
    pause_all: bool


HealthDetails.model_rebuild()
SlateLeague.model_rebuild()
