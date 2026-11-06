"""What the ticket engine takes in and gives back (docs/06 sections 5.1 and 5.4, D70)."""

from enum import StrEnum
from typing import Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from hoopsedge.models.calibration import Calibrator

League = Literal["NBA", "NCAA"]
Family = Literal["handicap", "total_ft", "total_1h", "team_total"]
Team = Literal["home", "away"]
Side = Literal["home", "away", "over", "under"]
EdgeLabel = Literal["measured", "unproven"]

FAMILIES: tuple[str, ...] = get_args(Family)
NOT_STARTED = "not_started"


class Reason(StrEnum):
    """Why an outcome or market didn't become a leg. Every rejection has exactly one."""

    UNMATCHED = "UNMATCHED"
    STARTED = "STARTED"
    TOO_CLOSE = "TOO_CLOSE"
    MARKET_DISABLED = "MARKET_DISABLED"
    NO_THRESHOLD = "NO_THRESHOLD"
    NO_BOOKABLE_LINE = "NO_BOOKABLE_LINE"
    STALE_ODDS = "STALE_ODDS"
    MISSING_PRED = "MISSING_PRED"
    NBA_INJURY_UNCERTAIN = "NBA_INJURY_UNCERTAIN"
    LOW_PROB = "LOW_PROB"
    LOW_EDGE = "LOW_EDGE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    SAME_GAME = "SAME_GAME"
    BELOW_CUTOFF = "BELOW_CUTOFF"
    TICKET_TOO_SHORT = "TICKET_TOO_SHORT"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Outcome(_Frozen):
    side: Side
    outcome_id: str
    odds: float = Field(gt=1.0)


class Offer(_Frozen):
    """One line of one market on one event, from Phase 6's normalizer.

    `home` means the canonical home team: the normalizer flips a swapped neutral-site event.
    """

    league: League
    event_id: str
    game_id: str | None  # None until matched (invariant 5).
    family: Family
    market_id: str
    specifier: str
    line: float  # On the home team for handicaps: home -5.5 is -5.5.
    team: Team | None = None  # Only for team totals.
    outcomes: tuple[Outcome, Outcome]
    tip_time: AwareDatetime
    status: str  # The event's. `not_started` is the only one that can become a leg.
    bookable: bool  # The market's: open, with every outcome active.
    favourite: bool  # SportyBet's flag, used only to break ties for the main line (D32).
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def _fits_its_family(self) -> Self:
        if (self.family == "team_total") != (self.team is not None):
            raise ValueError(f"{self.event_id}: `team` is for team totals, and they require it")
        expected = {"home", "away"} if self.family == "handicap" else {"over", "under"}
        if {outcome.side for outcome in self.outcomes} != expected:
            raise ValueError(f"{self.event_id}: {self.family} needs outcomes {sorted(expected)}")
        return self


class PeriodPrediction(_Frozen):
    mu_home: float
    mu_away: float
    sd_margin: float = Field(gt=0.0)
    sd_total: float = Field(gt=0.0)
    sd_home: float = Field(gt=0.0)
    sd_away: float = Field(gt=0.0)


class Confidence(_Frozen):
    games_home: int
    games_away: int
    prior_weight_home: float
    prior_weight_away: float


class InjuryStatus(_Frozen):
    """Worked out by the caller, which decides who counts as a questionable starter (D29, D56)."""

    questionable_starter: bool
    snapshot_at: AwareDatetime
    listed: tuple[dict[str, str], ...] = ()  # Each player listed, for the game page.


class Prediction(_Frozen):
    game_id: str
    league: League
    model_version: str
    feature_hash: str
    ft: PeriodPrediction | None
    h1: PeriodPrediction | None
    confidence: Confidence
    injury: InjuryStatus | None  # None: no snapshot, which the NBA gate treats as uncertain.
    key_features: dict[str, float | None] = {}  # Ratings and rest, for the game page.


class MarketSettings(_Frozen):
    enabled: bool  # What's in force, after any owner override (D65).
    passed_gate: bool  # What the evidence said, whatever is in force.
    edge_label: EdgeLabel
    min_prob: float | None  # None: no threshold met the leg floor, so no legs (D69).
    min_edge: float | None


class LeagueSettings(_Frozen):
    enabled: bool
    max_legs_per_ticket: int = Field(gt=0)
    max_tickets_per_market: int = Field(gt=0)
    max_legs_per_market: int | None = Field(default=None, gt=0)

    @property
    def most_legs(self) -> int:
        most = self.max_legs_per_ticket * self.max_tickets_per_market
        return most if self.max_legs_per_market is None else min(most, self.max_legs_per_market)


class EngineConfig(_Frozen):
    leagues: dict[str, LeagueSettings]
    markets: dict[str, dict[str, MarketSettings]]
    calibrators: dict[str, dict[str, Calibrator]]
    min_legs: int = Field(default=2, gt=0)
    min_minutes_before_tip: int = 20
    max_offer_age_seconds: int = 300
    allow_alt_lines: bool = False
    min_team_games_for_full_confidence: dict[str, int] = {"NBA": 10, "NCAA": 8}
    low_confidence_prior_weight: float = 0.6
    low_confidence_extra_prob: float = 0.04
    injury_snapshot_hours_before_tip: float = 3.0


class Leg(_Frozen):
    rank: int  # Across the whole market, so ticket 2 starts at 21.
    league: League
    game_id: str
    event_id: str
    family: Family
    market_id: str
    specifier: str
    line: float
    team: Team | None
    side: Side
    outcome_id: str
    odds: float
    p_raw: float
    p_calibrated: float
    p_win: float
    p_push: float
    p_lose: float
    fair: float
    edge: float
    ev: float
    tip_time: AwareDatetime
    # What grading compares the result with: margin, total or team total, by family.
    predicted_mean: float | None = None
    # Invariant 6: what produced this leg.
    model_version: str
    feature_hash: str
    offer_fetched_at: AwareDatetime


class Ticket(_Frozen):
    league: League
    family: Family
    ticket_no: int
    legs: tuple[Leg, ...]
    combined_odds: float
    combined_prob: float
    expected_value: float
    selection_hash: str
    passed_gate: bool
    edge_label: EdgeLabel


class Rejection(_Frozen):
    league: League
    game_id: str | None
    event_id: str
    family: Family
    reason: Reason
    # Empty when the whole market was rejected before any line or outcome was looked at.
    market_id: str | None = None
    specifier: str | None = None
    side: Side | None = None
    p_calibrated: float | None = None
    edge: float | None = None


class ScoredLine(_Frozen):
    """One main line's raw probabilities, whether or not any leg came of it (docs/08 section 7.3).

    This is what real-line calibrators are fit on. Above means home covers, or over.
    """

    league: League
    game_id: str
    event_id: str
    family: Family
    market_id: str
    specifier: str
    line: float
    team: Team | None
    p_above: float
    p_push: float
    fair_above: float
    tip_time: AwareDatetime
    fetched_at: AwareDatetime
    model_version: str


class BuildResult(_Frozen):
    tickets: tuple[Ticket, ...]
    rejections: tuple[Rejection, ...]
    assumptions: tuple[str, ...]
