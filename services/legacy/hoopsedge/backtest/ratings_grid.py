"""Tune the ratings' prior settings on replayed games (D58, D59; docs/05 early-season check).

Scores each setting by the ratings baseline's margin and total error, in early-season buckets, and
tests D59's explanation: if priors squeeze team gaps, the error grows with the expected margin, so
a line through actual margin against expected margin has a slope above 1.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from hoopsedge.features.training import ref_from_row, slate_run_time
from hoopsedge.ratings.engine import CONFIG, Engine
from hoopsedge.ratings.history import History

EARLY_DAYS = 14
SETTLED_DAYS = 45


@dataclass(frozen=True)
class Setting:
    r: float
    k: float
    hca_k: float


def _expected(engine: Engine, row: dict[str, Any], as_of: Any) -> tuple[float, float]:
    snap = engine.ratings_as_of(row["season"], as_of)
    league = snap.league
    ref = ref_from_row(row)
    home = engine.history.pooled_team(ref.season, ref.home_id)
    away = engine.history.pooled_team(ref.season, ref.away_id)
    edge = 0.0 if ref.neutral else league["hca"] / 2
    scale = (league["tau"] + snap.value("tempo", home) + snap.value("tempo", away)) / 100.0
    rate_home = league["mu"] + snap.value("off", home) - snap.value("def", away) + edge
    rate_away = league["mu"] + snap.value("off", away) - snap.value("def", home) - edge
    return (rate_home - rate_away) * scale, (rate_home + rate_away) * scale


def replay(history: History, setting: Setting, season: int) -> dict[str, np.ndarray]:
    """Pre-game expectations for every game of a season under one setting."""
    engine = Engine(history)
    engine.config = {**CONFIG[history.league], "r": setting.r, "k": setting.k,
                     "hca_k": setting.hca_k}  # fmt: skip
    games = history.games_frame(season)
    first: date = games["slate_date_et"].min()  # type: ignore[assignment]
    expected_margin, expected_total, actual_margin, actual_total, days, neutral = (
        [], [], [], [], [], []
    )  # fmt: skip
    for row in games.to_dicts():
        as_of = min(slate_run_time(history.league, row["slate_date_et"]), row["tip_utc"])
        margin, total = _expected(engine, row, as_of)
        expected_margin.append(margin)
        expected_total.append(total)
        actual_margin.append(row["home_pts"] - row["away_pts"])
        actual_total.append(row["home_pts"] + row["away_pts"])
        days.append((row["slate_date_et"] - first).days)
        neutral.append(row["neutral"])
    return {
        "expected_margin": np.array(expected_margin, dtype=float),
        "expected_total": np.array(expected_total, dtype=float),
        "actual_margin": np.array(actual_margin, dtype=float),
        "actual_total": np.array(actual_total, dtype=float),
        "days": np.array(days),
        "neutral": np.array(neutral, dtype=bool),
    }


def priced_d1_mask(history: History, season: int, priced: set[str]) -> np.ndarray:
    """Games that can become tickets: a real line and, for NCAA, D1 on both sides.

    Pooling in non-D1 visitors hides the squeeze this grid looks for: alone, those games have a
    slope near 0.4, and combined with the rest they pull 2026-27's slope down to about 1 (D60).
    """
    games = history.games_frame(season)
    d1 = history.d1_teams(season)
    return np.array(
        [
            game in priced and (d1 is None or (home in d1 and away in d1))
            for game, home, away in zip(
                games["game_id"].to_list(),
                games["home_id"].to_list(),
                games["away_id"].to_list(),
                strict=True,
            )
        ],
        dtype=bool,
    )


def summarize(
    result: dict[str, np.ndarray], first_day: int, last_day: int, mask: np.ndarray | None = None
) -> dict[str, float]:
    """Errors over a day range. `slope` above 1 means expected gaps were too small."""
    chosen = (result["days"] >= first_day) & (result["days"] <= last_day)
    if mask is not None:
        chosen = chosen & mask
    if not chosen.any():
        return {"games": 0}
    expected = result["expected_margin"][chosen]
    actual = result["actual_margin"][chosen]
    error = actual - expected
    slope = float(np.polyfit(expected, actual, 1)[0]) if len(expected) > 2 else float("nan")
    return {
        "games": int(chosen.sum()),
        "margin_mae": round(float(np.abs(error).mean()), 3),
        "margin_bias": round(float(error.mean()), 3),
        "slope": round(slope, 3),
        "total_mae": round(
            float(np.abs(result["actual_total"][chosen] - result["expected_total"][chosen]).mean()),
            3,
        ),
    }


def grid(history: History, settings: Iterable[Setting], seasons: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for setting in settings:
        for season in seasons:
            result = replay(history, setting, season)
            rows.append(
                {
                    "season": season,
                    "r": setting.r,
                    "k": setting.k,
                    "hca_k": setting.hca_k,
                    "early": summarize(result, 0, EARLY_DAYS - 1),
                    "middle": summarize(result, EARLY_DAYS, SETTLED_DAYS - 1),
                    "settled": summarize(result, SETTLED_DAYS, 10_000),
                    "all": summarize(result, 0, 10_000),
                }
            )
    return rows
