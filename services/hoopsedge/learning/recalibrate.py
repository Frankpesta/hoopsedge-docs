"""Nightly calibrators, fit only on what happened at real lines (docs/08 section 7.3, D18, D72).

For a clean comparison, each candidate is fit on days 15 to 60 and judged on the last 14 against
the calibrators in force. A family keeps what it has until a candidate beats it.

Full-game markets top up thin data with last season's walk-forward rows at real closing lines.
Half-time and Team O/U have no such rows, and their synthetic ones are the very thing D72 found
unreliable, so they wait for enough real SportyBet lines instead.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from hoopsedge.backtest.markets import FAMILIES
from hoopsedge.backtest.metrics import bootstrap_difference, log_loss
from hoopsedge.models.calibration import MIN_SAMPLES, Calibrator

CONFIDENCE = 0.90
WINDOW_DAYS = 60
EVALUATION_DAYS = 14
TOP_UP_BELOW = 1000
MIN_EVALUATION_ROWS = 50
MIN_REAL_ROWS_WITHOUT_TOP_UP = 300  # Half-time and Team O/U have nothing else to lean on.


@dataclass(frozen=True)
class FamilyResult:
    family: str
    adopted: bool
    reason: str
    rows: int = 0
    loss_current: float | None = None
    loss_candidate: float | None = None
    calibrator: Calibrator | None = None


def _losses(calibrator: Calibrator, rows: pl.DataFrame) -> np.ndarray:
    return log_loss(calibrator.apply(rows["p"].to_numpy()), rows["outcome"].to_numpy())


def _loss(calibrator: Calibrator, rows: pl.DataFrame) -> float:
    return float(_losses(calibrator, rows).mean())


def _fit(fit_rows: pl.DataFrame, extra: pl.DataFrame | None) -> Calibrator:
    frames = (
        [fit_rows] if extra is None or not len(extra) else [fit_rows, extra.select("p", "outcome")]
    )
    combined = pl.concat([frame.select("p", "outcome") for frame in frames])
    return Calibrator.fit(combined["p"].to_numpy(), combined["outcome"].to_numpy())


def recalibrate_family(
    family: str,
    rows: pl.DataFrame,
    current: Calibrator,
    today: date,
    top_up: pl.DataFrame | None = None,
) -> FamilyResult:
    """`rows` are real-line rows: `game_id`, `slate_date_et`, `p` and `outcome`."""
    if not len(rows):
        return FamilyResult(family, False, "No settled real-line rows yet")
    settled = rows.filter(
        pl.col("outcome").is_not_null()
        & (pl.col("slate_date_et") >= today - timedelta(days=WINDOW_DAYS))
        & (pl.col("slate_date_et") < today)
    )
    start = today - timedelta(days=EVALUATION_DAYS)
    evaluate = settled.filter(pl.col("slate_date_et") >= start)
    fit_rows = settled.filter(pl.col("slate_date_et") < start)
    count = len(settled)
    if len(evaluate) < MIN_EVALUATION_ROWS:
        reason = (
            f"{len(evaluate)} rows in the last {EVALUATION_DAYS} days, {MIN_EVALUATION_ROWS} needed"
        )
        return FamilyResult(family, False, reason, count)
    extra = top_up if top_up is not None and count < TOP_UP_BELOW else None
    if extra is None and count < MIN_REAL_ROWS_WITHOUT_TOP_UP:
        reason = f"{count} real rows, {MIN_REAL_ROWS_WITHOUT_TOP_UP} needed"
        return FamilyResult(family, False, reason, count)
    pool = len(fit_rows) + (0 if extra is None else len(extra))
    if pool < MIN_SAMPLES or len(np.unique(fit_rows["outcome"].to_numpy())) < 2:
        return FamilyResult(family, False, f"{pool} rows to fit on, {MIN_SAMPLES} needed", count)
    candidate = _fit(fit_rows, extra)
    loss_current = _loss(current, evaluate)
    loss_candidate = _loss(candidate, evaluate)
    # D9: measurably better. A lucky fortnight mustn't flip the calibrators back and forth nightly.
    gain = bootstrap_difference(
        evaluate["game_id"].to_numpy(),
        _losses(current, evaluate) - _losses(candidate, evaluate),
        level=CONFIDENCE,
    )
    if gain.low <= 0:
        reason = (
            f"Candidate log loss {loss_candidate:.5f} against {loss_current:.5f} doesn't clearly "
            f"beat it: the gain's interval reaches {gain.low:+.5f}"
        )
        return FamilyResult(family, False, reason, count, loss_current, loss_candidate)
    # The comparison is settled, so the calibrator in force can use every row.
    final = _fit(settled, extra)
    reason = f"Log loss {loss_current:.5f} to {loss_candidate:.5f} on {len(evaluate)} rows"
    return FamilyResult(family, True, reason, count, loss_current, loss_candidate, final)


def recalibrate(
    rows: pl.DataFrame,
    current: dict[str, Calibrator],
    today: date,
    top_ups: dict[str, pl.DataFrame],
) -> dict[str, FamilyResult]:
    results: dict[str, FamilyResult] = {}
    for family in FAMILIES:
        chosen = rows.filter(pl.col("family") == family) if len(rows) else rows
        results[family] = recalibrate_family(
            family, chosen, current[family], today, top_ups.get(family)
        )
    return results
