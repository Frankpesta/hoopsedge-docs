"""Market probabilities, pushes, no-vig and EV (docs/06 section 5.2)."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hoopsedge.markets.probability import (
    expected_value,
    fair_probability,
    score_two_way,
)
from hoopsedge.models.calibration import Calibrator
from hoopsedge.models.distribution import ScoreDistribution

IDENTITY = Calibrator("platt", (1.0,), (0.0,), 1000)


def _one(mu_home: float, mu_away: float) -> ScoreDistribution:
    def value(number: float) -> np.ndarray:
        return np.array([number])

    return ScoreDistribution(
        value(mu_home), value(mu_away), value(12.0), value(17.0), value(11.0), value(11.0)
    )


def test_an_integer_line_can_push_and_a_half_line_cannot() -> None:
    dist = _one(110.0, 105.0)
    assert dist.handicap(np.array([-5.0]))[1][0] > 0.02
    assert dist.handicap(np.array([-5.5]))[1][0] == 0.0
    assert dist.total(np.array([215.0]))[1][0] > 0.02
    assert dist.total(np.array([215.5]))[1][0] == 0.0
    assert dist.team_total("home", np.array([110.0]))[1][0] > 0.02


@given(
    st.floats(0.01, 0.97),
    st.floats(0.0, 0.03),
)
def test_each_side_adds_up_with_the_push(above: float, push: float) -> None:
    first, second = score_two_way(above, push, 1.0 - above - push, IDENTITY)
    assert first.p_win + first.p_push + first.p_lose == pytest.approx(1.0)
    assert second.p_win + second.p_push + second.p_lose == pytest.approx(1.0)
    assert first.p_win == pytest.approx(second.p_lose)
    assert first.p_push == second.p_push == pytest.approx(push)


def test_ranking_probability_removes_the_push() -> None:
    first, second = score_two_way(0.48, 0.04, 0.48, IDENTITY)
    assert first.p_raw == pytest.approx(0.5)
    assert first.p_calibrated == pytest.approx(0.5)
    assert first.p_win == pytest.approx(0.48)
    assert second.p_calibrated == pytest.approx(0.5)


def test_the_calibrator_sets_the_home_or_over_side_and_the_other_is_its_complement() -> None:
    # Phase 4 fit calibrators on the home and over sides only, so the engine must do the same.
    calibrator = Calibrator("isotonic", (0.3, 0.7), (0.4, 0.6), 1000)
    first, second = score_two_way(0.7, 0.0, 0.3, calibrator)
    assert first.p_calibrated == pytest.approx(0.6)
    assert second.p_calibrated == pytest.approx(0.4)


def test_fair_probability_removes_the_margin() -> None:
    assert fair_probability(1.91, 1.91) == pytest.approx(0.5)
    home = fair_probability(1.8, 2.05)
    assert home == pytest.approx((1 / 1.8) / (1 / 1.8 + 1 / 2.05))
    assert home + fair_probability(2.05, 1.8) == pytest.approx(1.0)


@given(st.floats(1.01, 50.0), st.floats(1.01, 50.0))
def test_fair_probabilities_always_add_to_one(first: float, second: float) -> None:
    assert fair_probability(first, second) + fair_probability(second, first) == pytest.approx(1.0)


def test_expected_value_ignores_stake_returned_on_a_push() -> None:
    assert expected_value(0.5, 0.5, 2.0) == pytest.approx(0.0)
    assert expected_value(0.48, 0.48, 2.0) == pytest.approx(0.0)
    assert expected_value(0.55, 0.45, 1.91) == pytest.approx(0.55 * 0.91 - 0.45)
