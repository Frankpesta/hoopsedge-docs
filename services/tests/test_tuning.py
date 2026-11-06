"""Hyperparameter search: bounded budget, repeatable, and validation-only (docs/05 section 4.2)."""

import polars as pl
import pytest

from hoopsedge.models.tuning import MAX_TRIALS, tune_league
from tests.model_data import feature_frame


def _split() -> tuple[pl.DataFrame, pl.DataFrame]:
    frame = feature_frame(days=60)
    return frame[:360], frame[360:]


def test_budget_above_the_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="trials"):
        tune_league(*_split(), trials=MAX_TRIALS + 1)


@pytest.fixture(scope="module")
def result() -> dict[str, object]:
    return tune_league(*_split(), trials=2, log=lambda _: None)


def test_every_family_gets_its_own_settings(result: dict[str, object]) -> None:
    params = result["params"]
    assert isinstance(params, dict)
    for family in ("ft_mean", "ft_var", "h1_mean", "h1_var"):
        assert "n_estimators" in params[family]


def test_all_forms_and_objectives_were_compared(result: dict[str, object]) -> None:
    tuning = result["tuning"]
    assert isinstance(tuning, dict)
    assert len(tuning["forms"]) == 8


def test_search_is_repeatable(result: dict[str, object]) -> None:
    assert tune_league(*_split(), trials=2, log=lambda _: None) == result
