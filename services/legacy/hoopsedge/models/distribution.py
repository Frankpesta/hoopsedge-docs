"""The score distribution of a game, and the probabilities it gives (docs/05 section 4.1).

Scores are whole numbers, so an integer line can push and uses a continuity correction. A half
line can't push, and its boundary already falls between two whole numbers.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import norm

Side = Literal["home", "away"]
Outcomes = tuple[np.ndarray, np.ndarray, np.ndarray]


def line_probabilities(mu: np.ndarray, sd: np.ndarray, line: np.ndarray) -> Outcomes:
    """P(above the line), P(exactly on it) and P(below it)."""
    whole = np.equal(np.mod(line, 1.0), 0.0)
    upper = np.where(whole, line + 0.5, line)
    lower = np.where(whole, line - 0.5, line)
    above = norm.sf((upper - mu) / sd)
    below = norm.cdf((lower - mu) / sd)
    push = np.where(whole, np.clip(1.0 - above - below, 0.0, 1.0), 0.0)
    return above, push, below


def ranking_probability(p_win: np.ndarray, p_push: np.ndarray) -> np.ndarray:
    """Win probability with the push removed. For ranking only (docs/06 section 5.2)."""
    return np.asarray(p_win / (1.0 - p_push))


def crps_normal(y: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    """Continuous ranked probability score of a normal forecast. Lower is better."""
    z = (y - mu) / sd
    return np.asarray(sd * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / np.sqrt(np.pi)))


def log_likelihood_normal(y: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    return np.asarray(norm.logpdf(y, mu, sd))


@dataclass(frozen=True)
class ScoreDistribution:
    """Bivariate normal for (home, away) points, over one period, for one or many games.

    Margin and total keep their own spreads, because those two are modelled directly. Team totals
    use each team's own spread.
    """

    mu_home: np.ndarray
    mu_away: np.ndarray
    sd_margin: np.ndarray
    sd_total: np.ndarray
    sd_home: np.ndarray
    sd_away: np.ndarray

    @property
    def mu_margin(self) -> np.ndarray:
        return np.asarray(self.mu_home - self.mu_away)

    @property
    def mu_total(self) -> np.ndarray:
        return np.asarray(self.mu_home + self.mu_away)

    @property
    def rho(self) -> np.ndarray:
        covariance = (self.sd_total**2 - self.sd_margin**2) / 4.0
        return np.asarray(np.clip(covariance / (self.sd_home * self.sd_away), -1.0, 1.0))

    def handicap(self, home_line: np.ndarray) -> Outcomes:
        """P(home covers), P(push), P(away covers). Home -5.5 is `home_line` -5.5."""
        return line_probabilities(self.mu_margin, self.sd_margin, -home_line)

    def total(self, line: np.ndarray) -> Outcomes:
        """P(over), P(push), P(under)."""
        return line_probabilities(self.mu_total, self.sd_total, line)

    def team_total(self, side: Side, line: np.ndarray) -> Outcomes:
        """P(over), P(push), P(under) on one team's points."""
        if side == "home":
            return line_probabilities(self.mu_home, self.sd_home, line)
        return line_probabilities(self.mu_away, self.sd_away, line)
