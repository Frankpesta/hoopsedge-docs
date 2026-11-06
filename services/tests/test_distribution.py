"""Distribution maths: known mean and spread give known probabilities (docs/05)."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import norm

from hoopsedge.models.distribution import (
    ScoreDistribution,
    crps_normal,
    line_probabilities,
    log_likelihood_normal,
    ranking_probability,
)


def _dist(**overrides: float) -> ScoreDistribution:
    values = {
        "mu_home": 110.0,
        "mu_away": 105.0,
        "sd_margin": 12.0,
        "sd_total": 18.0,
        "sd_home": 11.0,
        "sd_away": 11.0,
    }
    values.update(overrides)
    return ScoreDistribution(**{k: np.array([v]) for k, v in values.items()})


def test_half_point_line_has_no_push() -> None:
    over, push, under = line_probabilities(np.array([100.0]), np.array([10.0]), np.array([95.5]))
    assert push[0] == 0.0
    assert over[0] == pytest.approx(1 - norm.cdf(-0.45))
    assert over[0] + under[0] == pytest.approx(1.0)


def test_integer_line_uses_the_continuity_correction() -> None:
    over, push, under = line_probabilities(np.array([100.0]), np.array([10.0]), np.array([100.0]))
    assert push[0] == pytest.approx(norm.cdf(0.05) - norm.cdf(-0.05))
    assert over[0] == pytest.approx(1 - norm.cdf(0.05))
    assert under[0] == pytest.approx(norm.cdf(-0.05))


def test_handicap_is_on_the_margin() -> None:
    # Home -5.5: home covers when the margin is above 5.5. The model's margin is 5.
    home, push, away = _dist().handicap(np.array([-5.5]))
    assert push[0] == 0.0
    assert home[0] == pytest.approx(1 - norm.cdf(0.5 / 12))
    assert away[0] == pytest.approx(norm.cdf(0.5 / 12))


def test_handicap_integer_line_pushes_on_that_exact_margin() -> None:
    home, push, away = _dist().handicap(np.array([-5.0]))
    assert push[0] == pytest.approx(norm.cdf(0.5 / 12) - norm.cdf(-0.5 / 12))
    assert home[0] == pytest.approx(away[0])


def test_total_uses_the_total_spread() -> None:
    over, push, _ = _dist().total(np.array([220.5]))
    assert push[0] == 0.0
    assert over[0] == pytest.approx(1 - norm.cdf(5.5 / 18))


def test_team_totals_use_each_teams_own_marginal() -> None:
    over_home, _, _ = _dist(sd_home=9.0, sd_away=14.0).team_total("home", np.array([100.5]))
    over_away, _, _ = _dist(sd_home=9.0, sd_away=14.0).team_total("away", np.array([100.5]))
    assert over_home[0] == pytest.approx(1 - norm.cdf(-9.5 / 9))
    assert over_away[0] == pytest.approx(1 - norm.cdf(-4.5 / 14))


def test_correlation_follows_from_margin_and_total_spreads() -> None:
    # Var(total) - Var(margin) = 4 cov = 180, and Var(h) + Var(a) = (324 + 144) / 2 = 234.
    dist = _dist(sd_margin=12.0, sd_total=18.0, sd_home=np.sqrt(117.0), sd_away=np.sqrt(117.0))
    assert dist.rho[0] == pytest.approx(45.0 / 117.0)


def test_ranking_probability_removes_the_push() -> None:
    assert ranking_probability(np.array([0.45]), np.array([0.1]))[0] == pytest.approx(0.5)


def test_crps_of_a_perfect_point_is_the_known_constant() -> None:
    # For y = mu, CRPS = sigma * (2 phi(0) - 1 / sqrt(pi)).
    expected = 10.0 * (2 * norm.pdf(0) - 1 / np.sqrt(np.pi))
    assert crps_normal(np.array([100.0]), np.array([100.0]), np.array([10.0]))[0] == pytest.approx(
        expected
    )


def test_log_likelihood_matches_scipy() -> None:
    value = log_likelihood_normal(np.array([104.0]), np.array([100.0]), np.array([10.0]))[0]
    assert value == pytest.approx(norm.logpdf(104.0, 100.0, 10.0))


@given(
    mu=st.floats(60, 260),
    sd=st.floats(3, 40),
    line=st.integers(100, 500).map(lambda value: value / 2),
)
def test_outcomes_always_sum_to_one(mu: float, sd: float, line: float) -> None:
    over, push, under = line_probabilities(np.array([mu]), np.array([sd]), np.array([line]))
    assert over[0] + push[0] + under[0] == pytest.approx(1.0)
    assert min(over[0], push[0], under[0]) >= 0.0


@given(mu=st.floats(60, 260), sd=st.floats(3, 40), line=st.integers(100, 500))
def test_raising_the_line_never_raises_the_over(mu: float, sd: float, line: int) -> None:
    lines = np.array([line / 2, line / 2 + 0.5, line / 2 + 1.0])
    over, _, _ = line_probabilities(np.full(3, mu), np.full(3, sd), lines)
    assert over[0] >= over[1] >= over[2]
