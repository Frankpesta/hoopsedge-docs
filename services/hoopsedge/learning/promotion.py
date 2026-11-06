"""When a challenger may replace the champion (docs/08 section 7.3, D9, D16, C3, C15). Pure."""

from dataclasses import dataclass
from datetime import datetime

from hoopsedge.backtest.metrics import Interval

MIN_HOLDOUT_GAMES = {"NBA": 150, "NCAA": 400}
# C15: 21 days gives about 150 NBA games, so the All-Star break alone would block promotion.
HOLDOUT_DAYS = {"NBA": 28, "NCAA": 21}
BRIER_TOLERANCE = 0.002
CRPS_TOLERANCE = 0.01  # Docs/08's small epsilon, in points of CRPS.


@dataclass(frozen=True)
class Decision:
    promote: bool
    reasons: tuple[str, ...]


def fair_to_compare(champion_cutoff: datetime, holdout_start: datetime) -> bool:
    """D16: a champion that trained on the holdout would have an unfair advantage."""
    return champion_cutoff < holdout_start


def decide(
    league: str,
    games: int,
    log_likelihood: Interval,
    crps: Interval,
    brier_changes: dict[str, float],
) -> Decision:
    """Every number is the challenger's gain, except `brier_changes`, where higher is worse."""
    reasons: list[str] = []
    minimum = MIN_HOLDOUT_GAMES[league]
    if games < minimum:
        reasons.append(f"Holdout has {games} games, and {minimum} are needed")
    if log_likelihood.mean <= 0:
        reasons.append(f"Mean log-likelihood gain is {log_likelihood.mean:+.5f}, not above zero")
    elif log_likelihood.low <= 0 and crps.low <= -CRPS_TOLERANCE:
        reasons.append(
            f"Interval reaches {log_likelihood.low:+.5f} on log-likelihood and "
            f"{crps.low:+.5f} on CRPS"
        )
    worse = {name: change for name, change in brier_changes.items() if change > BRIER_TOLERANCE}
    for name, change in sorted(worse.items()):
        reasons.append(f"{name} Brier is worse by {change:.4f}")
    if reasons:
        return Decision(False, tuple(reasons))
    return Decision(True, (f"Log-likelihood gain {log_likelihood.mean:+.5f} on {games} games",))
