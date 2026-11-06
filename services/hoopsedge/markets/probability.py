"""A two-way market's probabilities, fair price and expected value (docs/06 section 5.2, D70).

Phase 4 fit calibrators and thresholds on the push-free probability of the home or over side, with
the other side as its complement. Scoring anything else here would put them on a different scale.
So the push is removed, the calibrator applied, and the push put back for `p_win` and `p_lose`.
"""

from dataclasses import dataclass

import numpy as np

from hoopsedge.models.calibration import Calibrator
from hoopsedge.models.distribution import ranking_probability


@dataclass(frozen=True)
class SideProbability:
    p_raw: float  # Before calibration, with the push removed.
    p_calibrated: float  # After calibration, with the push removed. Thresholds use this one.
    p_win: float
    p_push: float
    p_lose: float


def score_two_way(
    above: float, push: float, below: float, calibrator: Calibrator
) -> tuple[SideProbability, SideProbability]:
    """The home or over side, then the away or under side."""
    raw = float(ranking_probability(np.array([above]), np.array([push]))[0])
    calibrated = float(calibrator.apply(np.array([raw]))[0])
    settled = 1.0 - push
    first = SideProbability(raw, calibrated, calibrated * settled, push, (1 - calibrated) * settled)
    second = SideProbability(
        1 - raw, 1 - calibrated, (1 - calibrated) * settled, push, calibrated * settled
    )
    return first, second


def no_vig_probability(price: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Fair probability of `price` in a two-way market (docs/06 section 5.2)."""
    implied, implied_other = 1.0 / price, 1.0 / other
    return implied / (implied + implied_other)


def fair_probability(price: float, other: float) -> float:
    return float(no_vig_probability(np.array([price]), np.array([other]))[0])


def expected_value(p_win: float, p_lose: float, odds: float) -> float:
    """Per unit staked. A push returns the stake, so it adds nothing."""
    return p_win * (odds - 1.0) - p_lose
