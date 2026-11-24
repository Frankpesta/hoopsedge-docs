"""The L5 model (brief §4): pure, deterministic, every threshold from config.

No I/O and no clock. Inputs are plain values, so the same inputs always give the same output
(brief invariant 7), and the golden test can feed it the owner's sheet directly.

Leakage rule (invariant 1, brief invariant 6): a team's window holds only *completed* games of
the *given season* whose kickoff is *strictly before* the game being predicted.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np

from hoopsedge.common.config_v2 import OutlookBands, SportConfig, TierThresholds

# Edges are differences of averages, so float noise can sit on a threshold: 50.5 - 46.5 is 4.0,
# but (26.8 + 25.6) / 2 is not exactly 26.2. Values within EPS of a threshold count as on it.
EPS = 1e-9
Tier = Literal["Low", "Medium", "High"]
Lean = Literal["OVER", "UNDER", "PASS"]
Outlook = Literal["LOW", "MEDIUM", "HIGH"]
Side = Literal["home", "away"]
Direction = Literal["Over", "Under"]
CROSS_LEVEL_GAME = "cross_level_game"
CROSS_LEVEL_L5 = "cross_level_l5"
PROVISIONAL_TIERS = "provisional_tiers"
SKIP_MIN_GAMES = "min_games"


@dataclass(frozen=True)
class Result:
    """One completed game, as stored in `curated/results`."""

    season: int
    game_id: str
    kickoff_utc: datetime
    home_id: str
    away_id: str
    home_pts: int
    away_pts: int


@dataclass(frozen=True)
class TeamInput:
    team_id: str
    pf: float  # Mean points scored over the window.
    pa: float  # Mean points allowed.
    n: int
    game_ids: tuple[str, ...]  # Newest first.
    cross_level_opponents: int  # Games in the window against another level.


@dataclass(frozen=True)
class TeamPick:
    side: Side
    direction: Direction
    team_line: float
    proj: float
    edge: float  # proj - team_line


@dataclass(frozen=True)
class GamePrediction:
    home: TeamInput
    away: TeamInput
    eligible: bool
    skip_reason: str | None
    home_proj: float
    away_proj: float
    model_total: float
    sum_of_avgs: float
    home_tiers: tuple[Tier, Tier]  # (attack, defence)
    away_tiers: tuple[Tier, Tier]
    outlook: Outlook
    main_line: float | None
    edge: float | None  # model_total - main_line
    lean: Lean | None  # None when there is no main line.
    team_pick: TeamPick | None
    flags: tuple[str, ...] = field(default=())


def team_input(
    team_id: str,
    results: Iterable[Result],
    *,
    season: int,
    before: datetime,
    window: int,
    levels: dict[str, str] | None = None,
    same_level_only: bool = False,
) -> TeamInput:
    """The team's last `window` completed games of `season` that kicked off before `before`.

    `levels` maps team ID to level (FBS/FCS/OTHER). With `same_level_only`, games against an
    opponent of another level are left out of the window (brief §4.3).
    """
    if before.tzinfo is None:
        raise ValueError("`before` must be timezone-aware")
    known = levels or {}
    own_level = known.get(team_id)
    played: list[tuple[datetime, str, int, int, bool]] = []
    for game in results:
        if game.season != season or game.kickoff_utc >= before:
            continue
        if team_id == game.home_id:
            opponent, scored, allowed = game.away_id, game.home_pts, game.away_pts
        elif team_id == game.away_id:
            opponent, scored, allowed = game.home_id, game.away_pts, game.home_pts
        else:
            continue
        cross = levels is not None and known.get(opponent) != own_level
        if same_level_only and cross:
            continue
        played.append((game.kickoff_utc, game.game_id, scored, allowed, cross))
    played.sort(key=lambda row: (row[0], row[1]), reverse=True)
    recent = played[:window]
    n = len(recent)
    return TeamInput(
        team_id=team_id,
        pf=sum(row[2] for row in recent) / n if n else 0.0,
        pa=sum(row[3] for row in recent) / n if n else 0.0,
        n=n,
        game_ids=tuple(row[1] for row in recent),
        cross_level_opponents=sum(row[4] for row in recent),
    )


def attack_tier(pf: float, tiers: TierThresholds) -> Tier:
    low, high = tiers.atk
    if pf < low - EPS:
        return "Low"
    return "High" if pf >= high - EPS else "Medium"


def defence_tier(pa: float, tiers: TierThresholds) -> Tier:
    good, bad = tiers.defence
    if pa <= good + EPS:
        return "High"
    return "Low" if pa >= bad - EPS else "Medium"


_ATTACK_POINTS = {"Low": 0, "Medium": 1, "High": 2}
_DEFENCE_POINTS = {"High": 0, "Medium": 1, "Low": 2}  # A weak defence means more points.


def outlook(
    away_tiers: tuple[Tier, Tier], home_tiers: tuple[Tier, Tier], bands: OutlookBands
) -> Outlook:
    """Brief §4.4 and D82. Informational only: it never affects the lean."""
    score = sum(_ATTACK_POINTS[atk] + _DEFENCE_POINTS[dfn] for atk, dfn in (away_tiers, home_tiers))
    if score <= bands.low_max:
        return "LOW"
    return "MEDIUM" if score <= bands.medium_max else "HIGH"


def lean(edge: float, config: SportConfig) -> Lean:
    if edge >= config.over_threshold - EPS:
        return "OVER"
    if edge <= -config.under_threshold + EPS:
        return "UNDER"
    return "PASS"


def team_pick(
    home_proj: float,
    away_proj: float,
    home_line: float | None,
    away_line: float | None,
    threshold: float,
) -> TeamPick | None:
    """The side with the larger |proj - line| (brief §4.6), if it clears `threshold`.

    A projection exactly on its line points nowhere, so that side gives no pick, and two sides
    tied on |edge| give no pick either (audit item 16): neither is the better leg.
    """
    candidates: list[TeamPick] = []
    for side, proj, line in (("home", home_proj, home_line), ("away", away_proj, away_line)):
        if line is None:
            continue
        edge = proj - line
        if abs(edge) <= EPS:
            continue
        direction: Direction = "Over" if edge > 0 else "Under"
        candidates.append(TeamPick(side, direction, line, proj, edge))  # type: ignore[arg-type]
    if not candidates:
        return None
    candidates.sort(key=lambda pick: abs(pick.edge), reverse=True)
    if len(candidates) == 2 and abs(abs(candidates[0].edge) - abs(candidates[1].edge)) <= EPS:
        return None
    best = candidates[0]
    return best if abs(best.edge) >= threshold - EPS else None


def predict_game(
    home: TeamInput,
    away: TeamInput,
    *,
    config: SportConfig,
    bands: OutlookBands,
    tiers: TierThresholds,
    main_line: float | None,
    home_team_line: float | None = None,
    away_team_line: float | None = None,
    levels: dict[str, str] | None = None,
) -> GamePrediction:
    """Brief §4.2 to §4.6 for one game. `tiers` is the sport's fixed tiers or this run's
    provisional ones (see `provisional_tiers`)."""
    home_proj = (home.pf + away.pa) / 2
    away_proj = (away.pf + home.pa) / 2
    model_total = home_proj + away_proj
    home_tiers = (attack_tier(home.pf, tiers), defence_tier(home.pa, tiers))
    away_tiers = (attack_tier(away.pf, tiers), defence_tier(away.pa, tiers))

    flags: list[str] = []
    if levels is not None and levels.get(home.team_id) != levels.get(away.team_id):
        flags.append(CROSS_LEVEL_GAME)
    if home.cross_level_opponents or away.cross_level_opponents:
        flags.append(CROSS_LEVEL_L5)
    if tiers.provisional:
        flags.append(PROVISIONAL_TIERS)

    eligible = min(home.n, away.n) >= config.min_games
    edge = None if main_line is None else model_total - main_line
    pick = None
    if eligible:
        pick = team_pick(
            home_proj, away_proj, home_team_line, away_team_line, config.team_total_threshold
        )
    return GamePrediction(
        home=home,
        away=away,
        eligible=eligible,
        skip_reason=None if eligible else SKIP_MIN_GAMES,
        home_proj=home_proj,
        away_proj=away_proj,
        model_total=model_total,
        sum_of_avgs=away.pf + home.pf,
        home_tiers=home_tiers,
        away_tiers=away_tiers,
        outlook=outlook(away_tiers, home_tiers, bands),
        main_line=main_line,
        edge=edge,
        lean=None if edge is None or not eligible else lean(edge, config),
        team_pick=pick,
        flags=tuple(flags),
    )


def provisional_tiers(inputs: Sequence[TeamInput], min_games: int) -> TierThresholds:
    """NCAAB and NBA (brief §4.4, audit item 15): the 33rd and 67th percentiles of current PF and
    PA, over teams with at least `min_games`. The caller passes same-level teams, computed from
    games before the run. Defence uses the same cut points: a low PA is the good end."""
    usable = [team for team in inputs if team.n >= min_games]
    if len(usable) < 3:
        raise ValueError(f"Only {len(usable)} teams have {min_games}+ games: no tiers yet")
    pf = np.array([team.pf for team in usable])
    pa = np.array([team.pa for team in usable])
    atk = (float(np.percentile(pf, 100 / 3)), float(np.percentile(pf, 200 / 3)))
    dfn = (float(np.percentile(pa, 100 / 3)), float(np.percentile(pa, 200 / 3)))
    if not (atk[0] < atk[1] and dfn[0] < dfn[1]):
        raise ValueError("Tier percentiles coincide: the league's spread is too narrow yet")
    return TierThresholds.model_validate({"atk": atk, "def": dfn, "provisional": True})


def fixed_tiers(config: SportConfig) -> TierThresholds:
    if isinstance(config.tiers, str):
        raise ValueError("This sport's tiers are provisional: use provisional_tiers()")
    return config.tiers
