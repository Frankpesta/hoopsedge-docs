"""Scoring rules, bootstrap intervals, the market gate and the threshold sweep (docs/05)."""

from dataclasses import dataclass

import numpy as np
import polars as pl

EPSILON = 1e-6
BOOTSTRAP_RESAMPLES = 2000
PROB_GRID = tuple(round(0.52 + 0.01 * i, 2) for i in range(14))  # 0.52 to 0.65.
EDGE_GRID = tuple(round(0.005 * i, 3) for i in range(11))  # 0 to 0.05.


def log_loss(p: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, EPSILON, 1 - EPSILON)
    return np.asarray(-(outcome * np.log(clipped) + (1 - outcome) * np.log(1 - clipped)))


def brier(p: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    return np.asarray((p - outcome) ** 2)


@dataclass(frozen=True)
class Interval:
    mean: float
    low: float
    high: float
    count: int  # Whole games, the unit that gets resampled.


def bootstrap_difference(
    games: np.ndarray,
    values: np.ndarray,
    seed: int = 20261106,
    resamples: int = BOOTSTRAP_RESAMPLES,
    level: float = 0.95,
) -> Interval:
    """An interval for a mean, resampling whole games so one game's lines count once."""
    _, inverse = np.unique(games, return_inverse=True)
    sums = np.bincount(inverse, weights=values)
    sizes = np.bincount(inverse)
    per_game = sums / sizes
    count = len(per_game)
    rng = np.random.default_rng(seed)
    means = per_game[rng.integers(0, count, (resamples, count))].mean(axis=1)
    tail = (1.0 - level) * 50.0
    low, high = np.percentile(means, [tail, 100.0 - tail])
    return Interval(float(per_game.mean()), float(low), float(high), count)


def gate_decision(low: float, high: float) -> bool:
    """Open a market only if the model's improvement has an interval entirely above zero."""
    return low > 0 and high > 0


@dataclass(frozen=True)
class Thresholds:
    min_prob: float
    min_edge: float
    log_likelihood: float
    legs: int
    legs_per_slate: float


def sweep_thresholds(frame: pl.DataFrame, min_legs_per_slate: float) -> Thresholds | None:
    """Best calibrated log-likelihood of selected legs, among settings giving enough legs.

    Slates count if any leg was on offer, so a quiet night still lowers the average. Returns None
    when no setting gives enough legs: that's a finding to report, not something to paper over.
    """
    usable = frame.filter(pl.col("outcome").is_not_null())
    slates = usable["slate_date_et"].n_unique()
    if not slates:
        return None
    p = usable["p_calibrated"].to_numpy()
    edge = usable["edge"].to_numpy()
    outcome = usable["outcome"].to_numpy()
    best: Thresholds | None = None
    for min_prob in PROB_GRID:
        for min_edge in EDGE_GRID:
            chosen = (p >= min_prob) & (edge >= min_edge)
            legs = int(chosen.sum())
            if not legs or legs / slates < min_legs_per_slate:
                continue
            score = float(-log_loss(p[chosen], outcome[chosen]).mean())
            if best is None or score > best.log_likelihood:
                best = Thresholds(min_prob, min_edge, score, legs, legs / slates)
    return best


def flat_stake_return(outcome: np.ndarray, price: float = 1.0 + 100.0 / 110.0) -> np.ndarray:
    """Profit per unit staked, at -110-equivalent odds unless told otherwise."""
    return np.where(outcome == 1.0, price - 1.0, -1.0)
