"""Curated table rows (docs/12-data-model.md section 3)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Game(_Row):
    league: str
    season: int
    game_id: str
    tip_utc: datetime
    slate_date_et: date
    home_id: str
    away_id: str
    neutral: bool
    venue_id: str | None
    status: str
    home_pts: int
    away_pts: int
    home_1h: int
    away_1h: int
    home_periods: list[int]
    away_periods: list[int]
    ot_periods: int
    postseason: bool
    season_flag: str | None
    box_missing: bool


class TeamGame(_Row):
    game_id: str
    team_id: str
    opp_id: str
    is_home: bool
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    oreb: int
    dreb: int
    tov: int
    pf: int
    pts: int
    pts_1h: int
    minutes: int
    poss: float


class PlayerGame(_Row):
    game_id: str
    team_id: str
    player_id: str
    minutes: int
    pts: int
    starter: bool


class Reject(_Row):
    """A row held back from a curated table, with the reason (docs/03 section 2.3)."""

    table: str
    league: str
    game_id: str
    reason: str
    detail: str | None = None
