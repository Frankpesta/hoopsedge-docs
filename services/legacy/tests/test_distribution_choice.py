"""The alternative forms must win on evidence, or the normal stays (docs/05 section 4.1)."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from hoopsedge.backtest.distribution_choice import (
    compare_forms,
    crps_empirical,
    ot_mixture_log_likelihood,
)
from hoopsedge.models.distribution import crps_normal


def _rows(error_home: np.ndarray, error_away: np.ndarray) -> pl.DataFrame:
    count = len(error_home)
    return pl.DataFrame(
        {
            "game_id": [str(i) for i in range(count)],
            "slate_date_et": [date(2026, 1, 1) + timedelta(days=i // 10) for i in range(count)],
            "model": ["model"] * count,
            "period": ["ft"] * count,
            "mu_home": np.full(count, 110.0),
            "mu_away": np.full(count, 105.0),
            "sd_margin": np.full(count, 14.0),
            "sd_total": np.full(count, 14.0),
            "actual_home": 110.0 + error_home,
            "actual_away": 105.0 + error_away,
        }
    )


def test_empirical_crps_matches_the_normal_when_the_shape_is_normal() -> None:
    from scipy.stats import norm

    shape = norm.ppf(np.linspace(0.0005, 0.9995, 1999))
    y, mu, sd = np.array([104.0]), np.array([100.0]), np.array([10.0])
    assert crps_empirical(y, mu, sd, shape)[0] == pytest.approx(crps_normal(y, mu, sd)[0], abs=0.02)


def test_normal_stays_when_errors_really_are_normal() -> None:
    rng = np.random.default_rng(1)
    rows = _rows(rng.normal(0, 10, 3000), rng.normal(0, 10, 3000))
    assert compare_forms(rows, 12.0)["distribution"] == "normal"


def test_empirical_wins_on_clearly_non_normal_errors() -> None:
    rng = np.random.default_rng(2)
    skewed = rng.exponential(10, (2, 6000)) - 10
    assert compare_forms(_rows(skewed[0], skewed[1]), 12.0)["distribution"] == "empirical"


def test_mixture_equals_the_normal_with_no_chance_of_a_tie() -> None:
    from scipy.stats import norm

    total, mu, sd = np.array([220.0]), np.array([215.0]), np.array([15.0])
    value = ot_mixture_log_likelihood(total, mu, sd, np.array([500.0]), np.array([1.0]), 12.0)
    assert value[0] == pytest.approx(norm.logpdf(220.0, 215.0, 15.0))
