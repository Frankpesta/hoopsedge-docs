"""The v2 config document (brief §10): one validated document, every threshold in it.

Nothing in the L5 model or the ticket engine hard-codes a threshold (brief invariant 7); it reads
this. `load_defaults()` gives version 0 from `config_v2_defaults.yaml`. Storing versions and
editing them from the dashboard come in B4.
"""

from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULTS = Path(__file__).with_name("config_v2_defaults.yaml")
PROVISIONAL = "provisional_percentiles"
Sport = Literal["NCAAF", "NCAAB", "NBA"]
# Owner, 2026-11-24 (D101): book only the lowest-odds line. `main` decides the lean and is graded
# as evidence, but it is never booked, so it isn't a mode.
LineMode = Literal["safest", "safest_with_cushion"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TierThresholds(_Strict):
    """Attack: Low < atk[0] <= Medium < atk[1] <= High.
    Defence (points allowed, lower is better): High <= def[0] < Medium < def[1] <= Low."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    atk: tuple[float, float]
    defence: tuple[float, float] = Field(alias="def")
    provisional: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> "TierThresholds":
        for name, (low, high) in (("atk", self.atk), ("def", self.defence)):
            if not low < high:
                raise ValueError(f"{name} tier edges must rise: {low} then {high}")
        return self


class EspnSource(_Strict):
    path: str
    groups: list[int]


class LineModes(_Strict):
    totals: LineMode
    team_totals: LineMode


class SportConfig(_Strict):
    enabled: bool
    sport_id: str  # SportyBet's, e.g. sr:sport:16 (brief §3).
    tournament_ids: list[str]
    espn: EspnSource
    l5_window: int = Field(ge=1)
    min_games: int = Field(ge=1)
    l5_same_level_only: bool
    over_threshold: float = Field(ge=0)
    under_threshold: float = Field(ge=0)
    team_total_threshold: float = Field(ge=0)
    tiers: TierThresholds | Literal["provisional_percentiles"]
    line_mode: LineModes
    min_cushion: float = Field(ge=0)

    @model_validator(mode="after")
    def _window_holds_min_games(self) -> "SportConfig":
        if self.min_games > self.l5_window:
            raise ValueError(f"min_games {self.min_games} exceeds l5_window {self.l5_window}")
        return self


class OutlookBands(_Strict):
    """Tier score 0..8: LOW if <= low_max, MEDIUM up to medium_max, HIGH above (brief §4.4)."""

    low_max: int = Field(ge=0, le=8)
    medium_max: int = Field(ge=0, le=8)

    @model_validator(mode="after")
    def _ordered(self) -> "OutlookBands":
        if not self.low_max < self.medium_max:
            raise ValueError("outlook low_max must be below medium_max")
        return self


class TicketConfig(_Strict):
    max_legs: int = Field(ge=1)
    min_legs: int = Field(ge=1)  # D4: a shorter ticket isn't booked.
    max_games_per_kind: int = Field(ge=1)
    booking_cutoff_minutes: int = Field(ge=20)  # Invariant 2 and D86: never below 20.
    kinds: dict[Literal["TOTALS_LEAN", "TEAM_TOTALS", "TOTALS_ALL_OVER", "TOTALS_ALL_UNDER"], bool]


class V2Config(_Strict):
    sports: dict[Sport, SportConfig]
    outlook: OutlookBands
    tickets: TicketConfig
    timezone_display: str
    dry_run: bool

    @field_validator("sports")
    @classmethod
    def _all_sports(cls, sports: dict[str, SportConfig]) -> dict[str, SportConfig]:
        missing = {"NCAAF", "NCAAB", "NBA"} - set(sports)
        if missing:
            raise ValueError(f"config is missing sports: {sorted(missing)}")
        return sports


def parse_config(document: dict[str, Any]) -> V2Config:
    return V2Config.model_validate(document)


@cache
def load_defaults() -> V2Config:
    document: dict[str, Any] = yaml.safe_load(DEFAULTS.read_text("utf-8"))
    return parse_config(document)
