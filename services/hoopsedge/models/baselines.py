"""The baselines every model must beat (docs/05 section 4.4).

Each takes training rows and rows to predict. Spreads are league constants from the training rows
only (D17), so all candidates are scored on an equal footing.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl

from hoopsedge.models.distribution import ScoreDistribution
from hoopsedge.ratings.engine import Engine

Period = Literal["ft", "h1"]
TARGETS: dict[str, tuple[str, str]] = {
    "ft": ("target_home_pts", "target_away_pts"),
    "h1": ("target_home_1h", "target_away_1h"),
}
RATE_INPUTS: dict[str, tuple[str, str, str]] = {
    "ft": ("exp_ppp_h", "exp_ppp_a", "exp_poss"),
    "h1": ("exp_ppp_1h_h", "exp_ppp_1h_a", "exp_poss_1h"),
}
MIN_SPREAD = 1.0


def targets_of(frame: pl.DataFrame, period: str) -> tuple[np.ndarray, np.ndarray]:
    home, away = TARGETS[period]
    return (
        frame[home].cast(pl.Float64).to_numpy(),
        frame[away].cast(pl.Float64).to_numpy(),
    )


def rate_means(frame: pl.DataFrame, period: str) -> tuple[np.ndarray, np.ndarray]:
    home, away, poss = RATE_INPUTS[period]
    scale = frame[poss].to_numpy() / 100.0
    return frame[home].to_numpy() * scale, frame[away].to_numpy() * scale


@dataclass(frozen=True)
class Spreads:
    """One value per league: what the residuals of this baseline were in training."""

    margin: float
    total: float
    home: float
    away: float

    @classmethod
    def from_residuals(
        cls, home: np.ndarray, away: np.ndarray, mean_home: np.ndarray, mean_away: np.ndarray
    ) -> "Spreads":
        usable = np.isfinite(home) & np.isfinite(away) & np.isfinite(mean_home + mean_away)
        if not usable.any():
            raise ValueError("No training rows with both a score and a prediction")
        error_home = home[usable] - mean_home[usable]
        error_away = away[usable] - mean_away[usable]
        return cls(
            max(float(np.std(error_home - error_away)), MIN_SPREAD),
            max(float(np.std(error_home + error_away)), MIN_SPREAD),
            max(float(np.std(error_home)), MIN_SPREAD),
            max(float(np.std(error_away)), MIN_SPREAD),
        )

    def distribution(self, mean_home: np.ndarray, mean_away: np.ndarray) -> ScoreDistribution:
        count = len(mean_home)
        return ScoreDistribution(
            mean_home,
            mean_away,
            np.full(count, self.margin),
            np.full(count, self.total),
            np.full(count, self.home),
            np.full(count, self.away),
        )


def ratings_baseline(train: pl.DataFrame, rows: pl.DataFrame, period: str) -> ScoreDistribution:
    """Baseline 1: ratings only, `mu + O - D + HCA` times expected possessions. No ML."""
    home, away = targets_of(train, period)
    spreads = Spreads.from_residuals(home, away, *rate_means(train, period))
    return spreads.distribution(*rate_means(rows, period))


def _elo_inputs(frame: pl.DataFrame) -> np.ndarray:
    return np.column_stack(
        [np.ones(len(frame)), frame["elo_diff"].to_numpy(), 1.0 - frame["neutral"].to_numpy()]
    )


def elo_baseline(train: pl.DataFrame, rows: pl.DataFrame, period: str) -> ScoreDistribution:
    """Baseline 2: margin from Elo alone, and the league's average total for every game."""
    home, away = targets_of(train, period)
    usable = np.isfinite(home) & np.isfinite(away)
    inputs = _elo_inputs(train)[usable]
    coefficients, *_ = np.linalg.lstsq(inputs, (home - away)[usable], rcond=None)
    average_total = float(np.mean((home + away)[usable]))

    def means(frame: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        margin = _elo_inputs(frame) @ coefficients
        return (average_total + margin) / 2.0, (average_total - margin) / 2.0

    spreads = Spreads.from_residuals(home, away, *means(train))
    return spreads.distribution(*means(rows))


def prior_only_columns(engine: Engine, season: int) -> pl.DataFrame:
    """Baseline 3's means: last season's regressed ratings, with no update from this season.

    All null for a season with no previous one, which then has nothing to compare against.
    """
    history = engine.history
    games = history.games_frame(season)
    has_prior = season - 1 in history.seasons()
    priors = engine.priors_for(season)
    league = priors.league
    columns: dict[str, list[float | None]] = {
        f"prior_{side}_{period}": [] for side in ("home", "away") for period in ("ft", "h1")
    }
    for row in games.to_dicts():
        if not has_prior:
            for values in columns.values():
                values.append(None)
            continue
        home = history.pooled_team(season, row["home_id"])
        away = history.pooled_team(season, row["away_id"])
        edge = 0.0 if row["neutral"] else league["hca"] / 2
        for period, mu, off, defence, tempo, tau in (
            ("ft", "mu", "off", "def", "tempo", "tau"),
            ("h1", "mu_1h", "off_1h", "def_1h", "tempo_1h", "tau_1h"),
        ):
            scale = (league[tau] + priors.value(tempo, home) + priors.value(tempo, away)) / 100.0
            rate_home = league[mu] + priors.value(off, home) - priors.value(defence, away) + edge
            rate_away = league[mu] + priors.value(off, away) - priors.value(defence, home) - edge
            columns[f"prior_home_{period}"].append(rate_home * scale)
            columns[f"prior_away_{period}"].append(rate_away * scale)
    return pl.DataFrame(
        {"game_id": games["game_id"], **columns},
        schema_overrides={name: pl.Float64 for name in columns},
    )


def prior_baseline(train: pl.DataFrame, rows: pl.DataFrame, period: str) -> ScoreDistribution:
    """Baseline 3. Rows need the `prior_*` columns. Null means no prediction for that game."""

    def means(frame: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return (
            frame[f"prior_home_{period}"].cast(pl.Float64).to_numpy(),
            frame[f"prior_away_{period}"].cast(pl.Float64).to_numpy(),
        )

    home, away = targets_of(train, period)
    spreads = Spreads.from_residuals(home, away, *means(train))
    return spreads.distribution(*means(rows))
