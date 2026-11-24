"""Champion against challenger on games neither trained on (docs/08 section 7.3, D16, D17)."""

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import polars as pl

from hoopsedge.backtest.markets import FAMILIES, SYNTHETIC, score_markets
from hoopsedge.backtest.metrics import Interval, bootstrap_difference, brier
from hoopsedge.backtest.walkforward import _rows
from hoopsedge.models.baselines import ratings_baseline, targets_of
from hoopsedge.models.distribution import (
    ScoreDistribution,
    crps_normal,
    log_likelihood_normal,
)
from hoopsedge.models.training import PERIODS, usable_rows

CONFIDENCE = 0.90  # Docs/08's paired bootstrap.
CHAMPION = "champion"
CHALLENGER = "challenger"
BASELINE = "baseline"


class Predicts(Protocol):
    def predict(self, frame: pl.DataFrame, period: str) -> ScoreDistribution: ...


@dataclass(frozen=True)
class HoldoutMetrics:
    games: int
    log_likelihood: Interval
    crps: Interval
    brier_changes: dict[str, float]
    brier_scores: dict[str, dict[str, float]]

    def to_json(self) -> dict[str, Any]:
        def interval(item: Interval) -> dict[str, float]:
            return {"mean": item.mean, "low": item.low, "high": item.high}

        return {
            "games": self.games,
            "log_likelihood_gain": interval(self.log_likelihood),
            "crps_gain": interval(self.crps),
            "brier_changes": self.brier_changes,
            "brier_scores": self.brier_scores,
        }


def _per_game(rows: pl.DataFrame, dist: ScoreDistribution, period: str) -> tuple[Any, Any]:
    """Log-likelihood and CRPS for margin and total, each game's two averaged."""
    home, away = targets_of(rows, period)
    margin, total = home - away, home + away
    likelihood = (
        log_likelihood_normal(margin, dist.mu_margin, dist.sd_margin)
        + log_likelihood_normal(total, dist.mu_total, dist.sd_total)
    ) / 2.0
    crps = (
        crps_normal(margin, dist.mu_margin, dist.sd_margin)
        + crps_normal(total, dist.mu_total, dist.sd_total)
    ) / 2.0
    return likelihood, crps


def _shared_line_brier(
    train: pl.DataFrame,
    rows: pl.DataFrame,
    league: str,
    champion: Predicts,
    challenger: Predicts,
) -> dict[str, dict[str, float]]:
    """D17: both on the same synthetic lines, so neither is scored at its own median."""
    parts: list[pl.DataFrame] = []
    for period in PERIODS:
        usable = usable_rows(rows, period)
        parts += [
            _rows(usable, league, CHAMPION, period, champion.predict(usable, period)),
            _rows(usable, league, CHALLENGER, period, challenger.predict(usable, period)),
            _rows(usable, league, BASELINE, period, ratings_baseline(train, usable, period)),
        ]
    predictions = pl.concat(parts)
    no_real_lines = pl.DataFrame(
        schema={
            "game_id": pl.String,
            "spread_home": pl.Float64,
            "total": pl.Float64,
            "price_home": pl.Float64,
            "price_away": pl.Float64,
            "price_over": pl.Float64,
            "price_under": pl.Float64,
        }
    )
    scored = score_markets(predictions, no_real_lines).filter(
        (pl.col("source") == SYNTHETIC) & pl.col("outcome").is_not_null()
    )
    result: dict[str, dict[str, float]] = {}
    for family in FAMILIES:
        result[family] = {}
        for model in (CHAMPION, CHALLENGER):
            chosen = scored.filter((pl.col("family") == family) & (pl.col("model") == model))
            if len(chosen):
                values = brier(chosen["p"].to_numpy(), chosen["outcome"].to_numpy())
                result[family][model] = float(values.mean())
    return result


def holdout_metrics(
    train: pl.DataFrame,
    holdout: pl.DataFrame,
    league: str,
    champion: Predicts,
    challenger: Predicts,
) -> HoldoutMetrics:
    gains_likelihood: list[np.ndarray] = []
    gains_crps: list[np.ndarray] = []
    games: list[np.ndarray] = []
    for period in PERIODS:
        rows = usable_rows(holdout, period)
        old_like, old_crps = _per_game(rows, champion.predict(rows, period), period)
        new_like, new_crps = _per_game(rows, challenger.predict(rows, period), period)
        gains_likelihood.append(new_like - old_like)
        gains_crps.append(old_crps - new_crps)  # Lower CRPS is better.
        games.append(rows["game_id"].to_numpy())
    ids = np.concatenate(games)
    scores = _shared_line_brier(train, holdout, league, champion, challenger)
    changes = {
        family: pair[CHALLENGER] - pair[CHAMPION]
        for family, pair in scores.items()
        if CHAMPION in pair and CHALLENGER in pair
    }
    return HoldoutMetrics(
        games=len(set(ids.tolist())),
        log_likelihood=bootstrap_difference(
            ids, np.concatenate(gains_likelihood), level=CONFIDENCE
        ),
        crps=bootstrap_difference(ids, np.concatenate(gains_crps), level=CONFIDENCE),
        brier_changes=changes,
        brier_scores=scores,
    )
