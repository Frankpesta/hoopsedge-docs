"""Evidence tables: paired comparisons, the gate and the unproven label (docs/05, D17, D18)."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from hoopsedge.backtest import run
from hoopsedge.backtest.evidence import (
    apply_overrides,
    both_sides,
    calibrate_by_window,
    gates,
    market_table,
    selection_table,
)
from hoopsedge.backtest.markets import score_markets
from hoopsedge.backtest.plan import PLANS, Plan
from hoopsedge.backtest.walkforward import Window, run_walkforward
from hoopsedge.models.training import DEFAULT_PARAMS
from tests.model_data import feature_frame

FAST = {
    **DEFAULT_PARAMS,
    "mean": {**DEFAULT_PARAMS["mean"], "n_estimators": 20},
    "var": {**DEFAULT_PARAMS["var"], "n_estimators": 10},
}
START = date(2025, 11, 1)


def _scored() -> tuple[pl.DataFrame, Window]:
    frame = feature_frame(days=100).with_columns(
        pl.lit(110.0).alias("prior_home_ft"),
        pl.lit(106.0).alias("prior_away_ft"),
        pl.lit(55.0).alias("prior_home_h1"),
        pl.lit(53.0).alias("prior_away_h1"),
    )
    windows = [
        Window(START + timedelta(days=50), START + timedelta(days=75)),
        Window(START + timedelta(days=75), START + timedelta(days=100)),
    ]
    predictions = run_walkforward(frame, windows, FAST, "NBA")
    ids = predictions["game_id"].unique().sort().to_list()
    lines = pl.DataFrame(
        {
            "game_id": ids,
            "kind": ["close"] * len(ids),
            "spread_home": [-3.5] * len(ids),
            "total": [218.5] * len(ids),
            "price_home": [1.8] * len(ids),
            "price_away": [2.05] * len(ids),
            "price_over": [1.91] * len(ids),
            "price_under": [1.91] * len(ids),
        }
    )
    return score_markets(predictions, lines), windows[1]


def _calibrated() -> pl.DataFrame:
    scored, window = _scored()
    return calibrate_by_window(scored, [window.start], window.end)


def test_market_table_compares_every_candidate_on_paired_rows() -> None:
    table = market_table(_calibrated())
    families = {row["family"] for row in table}
    assert families == {"handicap", "total_ft", "total_1h", "team_total"}
    by_key: dict[tuple[str, str], set[int]] = {}
    for row in table:
        by_key.setdefault((row["family"], row["source"]), set()).add(row["rows"])
    assert all(len(sizes) == 1 for sizes in by_key.values())


def test_baseline_against_itself_shows_no_gain() -> None:
    table = market_table(_calibrated())
    for row in (r for r in table if r["model"] == "baseline"):
        assert row["gain_calibrated_vs_baseline"]["mean"] == 0.0


def test_families_without_real_lines_stay_unproven_even_when_they_pass() -> None:
    decisions = gates(market_table(_calibrated()))
    assert decisions["handicap"]["judged_on"] == "real"
    assert decisions["total_ft"]["judged_on"] == "real"
    assert decisions["total_1h"]["judged_on"] == "synthetic"
    assert decisions["team_total"]["judged_on"] == "synthetic"
    assert decisions["total_1h"]["edge"] == "unproven"
    assert decisions["team_total"]["edge"] == "unproven"


def test_a_broken_baseline_calibrator_cannot_hand_the_model_a_win() -> None:
    # Real case: a baseline at 0.666 raw and 0.722 calibrated made a small gain look large.
    rows = 400
    outcome = np.tile([1.0, 0.0], rows // 2)
    frame = pl.DataFrame(
        {
            "game_id": [str(i) for i in range(rows)] * 2,
            "family": ["handicap"] * (2 * rows),
            "source": ["real"] * (2 * rows),
            "selection": ["home"] * (2 * rows),
            "line": [-3.5] * (2 * rows),
            "model": ["model"] * rows + ["baseline"] * rows,
            "p": np.r_[np.where(outcome == 1, 0.55, 0.45), np.where(outcome == 1, 0.55, 0.45)],
            "p_calibrated": np.r_[
                np.where(outcome == 1, 0.55, 0.45),
                np.where(outcome == 1, 0.2, 0.8),  # Wrecked by its calibrator.
            ],
            "outcome": np.r_[outcome, outcome],
        }
    )
    table = market_table(frame)
    row = next(r for r in table if r["model"] == "model")
    assert row["gain_calibrated_vs_baseline"]["mean"] > 0.5
    assert row["gain_vs_best_baseline"]["mean"] == 0.0
    assert gates(table)["handicap"]["enabled"] is False


def test_owner_override_enables_a_market_but_keeps_its_failure_visible() -> None:
    decisions = apply_overrides(
        gates([]), {"handicap": {"enabled": True, "decision": "D65", "by": "owner"}}
    )
    handicap = decisions["handicap"]
    assert handicap["enabled"] is True
    assert handicap["passed"] is False  # What the evidence said never changes.
    assert handicap["override"] == {"enabled": True, "decision": "D65", "by": "owner"}
    assert handicap["reason"] == "No settled rows to judge"
    assert decisions["total_ft"]["enabled"] is False
    assert decisions["total_ft"]["passed"] is False
    assert decisions["total_ft"]["override"] is None


def test_an_override_with_no_decision_on_record_is_refused() -> None:
    with pytest.raises(ValueError, match="decision"):
        apply_overrides(gates([]), {"handicap": {"enabled": True}})


def test_an_override_for_an_unknown_market_is_refused() -> None:
    with pytest.raises(ValueError, match="moneyline"):
        apply_overrides(gates([]), {"moneyline": {"enabled": True, "decision": "D65"}})


def test_gate_opens_only_on_an_interval_above_zero() -> None:
    def row(low: float, high: float) -> dict[str, object]:
        return {
            "family": "handicap",
            "source": "real",
            "model": "model",
            "gain_vs_best_baseline": {"mean": 0.0, "low": low, "high": high, "games": 9},
        }

    assert gates([row(0.001, 0.01)])["handicap"]["enabled"] is True
    assert gates([row(-0.001, 0.01)])["handicap"]["enabled"] is False
    assert gates([])["handicap"]["enabled"] is False


def test_both_sides_use_each_sides_own_fair_price() -> None:
    sides = both_sides(_calibrated()).filter(
        (pl.col("family") == "handicap") & (pl.col("model") == "model")
    )
    home = sides.filter(pl.col("is_home_side")).row(0, named=True)
    away = sides.filter((~pl.col("is_home_side")) & (pl.col("game_id") == home["game_id"])).row(
        0, named=True
    )
    # Prices 1.80 and 2.05: fair home is (1/1.80) / (1/1.80 + 1/2.05).
    fair = (1 / 1.8) / (1 / 1.8 + 1 / 2.05)
    assert home["edge"] == np.float64(home["p_calibrated"] - fair) or np.isclose(
        home["edge"], home["p_calibrated"] - fair
    )
    assert np.isclose(away["edge"], away["p_calibrated"] - (1 - fair))
    assert np.isclose(away["p_calibrated"], 1 - home["p_calibrated"])
    assert away["outcome"] == 1 - home["outcome"]


def test_selection_only_counts_picks_passing_the_thresholds() -> None:
    table = selection_table(_calibrated(), {"handicap": {"min_prob": 0.99, "min_edge": 0.5}})
    assert all(row["picks"] == 0 for row in table)
    open_table = selection_table(_calibrated(), {"handicap": {"min_prob": 0.5, "min_edge": -1.0}})
    assert all(row["picks"] > 0 for row in open_table)


def test_thresholds_use_each_markets_own_leg_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    floors: dict[str, float] = {}

    def record(frame: pl.DataFrame, floor: float) -> None:
        floors[frame["family"][0]] = floor

    monkeypatch.setattr(run, "sweep_thresholds", record)
    scored, _ = _scored()
    plan = Plan(
        "NBA",
        validation=Window(START + timedelta(days=50), START + timedelta(days=100)),
        tests=(),
        refit_days=30,
        min_legs_per_slate=10.0,
        family_leg_floors={"handicap": 1.0},
    )
    run.validation_thresholds(scored, plan)
    assert floors == {"handicap": 1.0, "total_ft": 10.0, "total_1h": 10.0, "team_total": 10.0}


def test_only_the_owners_two_ncaa_markets_have_a_lower_floor() -> None:
    ncaa = PLANS["NCAA"]
    assert ncaa.leg_floor("handicap") == ncaa.leg_floor("total_ft") == 1.0
    assert ncaa.leg_floor("total_1h") == ncaa.leg_floor("team_total") == 10.0
    assert not PLANS["NBA"].family_leg_floors


def test_every_plan_validates_before_any_evidence() -> None:
    for plan in PLANS.values():
        first_test = min(test.first for test in plan.tests)
        assert plan.validation.end <= first_test


def test_test_windows_stop_at_the_newest_game() -> None:
    plan = PLANS["NCAA"]
    windows = plan.test_windows(date(2026, 11, 6))
    assert max(window.end for _, window in windows) == date(2026, 11, 7)
    assert {label for label, _ in windows} == {"2025-26", "2026-27 start"}
