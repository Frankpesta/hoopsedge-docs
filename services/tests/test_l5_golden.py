"""Golden test (brief §11.1): the L5 model reproduces the owner's 2026-11-21 NCAAF sheet.

Each row is recomputed from its own L5 PF/PA columns with the default NCAAF config, and the
sheet's "Vegas Total" as the main line.
"""

import csv
from pathlib import Path
from typing import Any, cast

import pytest

from hoopsedge.common.config_v2 import load_defaults
from hoopsedge.l5.model import (
    GamePrediction,
    TeamInput,
    Tier,
    fixed_tiers,
    outlook,
    predict_game,
)

SHEET = Path(__file__).resolve().parent / "fixtures" / "ncaaf_predictions_20261121.csv"
CONFIG = load_defaults()
NCAAF = CONFIG.sports["NCAAF"]
ROWS: list[dict[str, str]] = list(csv.DictReader(SHEET.open(encoding="utf-8")))


def team(name: str, pf_pa: str) -> TeamInput:
    pf, pa = (float(value) for value in pf_pa.split("/"))
    return TeamInput(name, pf, pa, n=5, game_ids=(), cross_level_opponents=0)


def predict(row: dict[str, str]) -> GamePrediction:
    return predict_game(
        team(row["Home"], row["Home L5 PF/PA"]),
        team(row["Away"], row["Away L5 PF/PA"]),
        config=NCAAF,
        bands=CONFIG.outlook,
        tiers=fixed_tiers(NCAAF),
        main_line=float(row["Vegas Total"]),
    )


def tiers_of(text: str) -> tuple[Tier, Tier]:
    attack, defence = text.split("/")
    return cast(Tier, attack), cast(Tier, defence)


def label(row: dict[str, str]) -> str:
    return f"{row['Away']} @ {row['Home']}"


def test_the_sheet_has_all_46_rows() -> None:
    assert len(ROWS) == 46


@pytest.mark.parametrize("row", ROWS, ids=label)
def test_model_total_and_sum_of_avgs(row: dict[str, str]) -> None:
    prediction = predict(row)
    assert prediction.model_total == pytest.approx(float(row["Model Total"]), abs=0.05 + 1e-9)
    assert prediction.sum_of_avgs == pytest.approx(float(row["Sum of Avgs"]), abs=0.05 + 1e-9)


@pytest.mark.parametrize("row", ROWS, ids=label)
def test_lean(row: dict[str, str]) -> None:
    assert predict(row).lean == row["Lean"]


@pytest.mark.parametrize("row", ROWS, ids=label)
def test_tiers(row: dict[str, str]) -> None:
    prediction = predict(row)
    assert prediction.away_tiers == tiers_of(row["Away Atk/Def"])
    assert prediction.home_tiers == tiers_of(row["Home Atk/Def"])


@pytest.mark.parametrize("row", ROWS, ids=label)
def test_glossary_outlook(row: dict[str, str]) -> None:
    from_sheet = outlook(
        tiers_of(row["Away Atk/Def"]), tiers_of(row["Home Atk/Def"]), CONFIG.outlook
    )
    assert from_sheet == row["Glossary Outlook"]
    assert predict(row).outlook == row["Glossary Outlook"]


@pytest.mark.parametrize(
    ("away", "home", "edge", "lean"),
    [
        ("Colorado St", "Fresno St", 3.7, "PASS"),
        ("South Florida", "FAU", 3.8, "PASS"),
        ("Colorado", "Cincinnati", 4.3, "OVER"),
        ("BYU", "Kansas", 4.9, "OVER"),
        ("South Alabama", "Southern Miss", -4.8, "PASS"),
        ("Houston", "West Virginia", -6.8, "UNDER"),
    ],
)
def test_the_edge_cases_that_pin_the_thresholds(
    away: str, home: str, edge: float, lean: str
) -> None:
    row = next(r for r in ROWS if r["Away"] == away and r["Home"] == home)
    prediction = predict(row)
    assert prediction.edge == pytest.approx(edge, abs=0.05 + 1e-9)
    assert prediction.lean == lean


def test_the_outlook_never_moves_the_lean() -> None:
    """Brief §4.4: same totals and line, different tiers, same lean."""
    changed: Any = CONFIG.outlook.model_copy(update={"low_max": 0, "medium_max": 1})
    for row in ROWS:
        moved = predict_game(
            team(row["Home"], row["Home L5 PF/PA"]),
            team(row["Away"], row["Away L5 PF/PA"]),
            config=NCAAF,
            bands=changed,
            tiers=fixed_tiers(NCAAF),
            main_line=float(row["Vegas Total"]),
        )
        assert moved.lean == row["Lean"]
