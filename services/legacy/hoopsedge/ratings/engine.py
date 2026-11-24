"""Point-in-time ratings (docs/04-ratings-features.md section 3.2).

`ratings_as_of` solves, from the games knowable at `as_of`:

    stat_ij = mu + O_i - D_j + h * site_i
    poss_ij = tau + T_i + T_j

with exponential recency weights and a ridge penalty toward each team's preseason prior, so a
team with n games has prior weight k / (k + n). `site` is +1/2 for home, -1/2 for away and 0 at a
neutral site, so `mu` is the league level and `h` is the full home-road gap.

Only teams already seen enter the solve, in sorted-ID order. Future games can't change even the
rounding of a result. Elo runs alongside as a robustness signal.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse as sp
import yaml
from scipy.sparse.linalg import splu

from hoopsedge.ratings.history import History, SeasonData
from hoopsedge.teams.registry import NON_D1

END_OF_TIME = datetime(2100, 1, 1, tzinfo=UTC)
MICROS_PER_DAY = 86_400_000_000
ELO_BASE = 1500.0
LEVEL_PENALTY = 1e-3  # Just enough to keep league terms at their priors before any game.
FACTOR_STATS = ("efg", "tov", "orb", "ftr", "par")
BOTTOM_QUARTILE = 0.25
# Offence and defence are solved on one design, so all these share a factorization.
OD_STATS = ("ppp", "ppp_1h", *FACTOR_STATS)
PRIOR_LEVELS = {"efg": 0.53, "tov": 0.14, "orb": 0.27, "ftr": 0.2, "par": 0.38}


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(
        (Path(__file__).parent / "config.yaml").read_text("utf-8")
    )
    return config


CONFIG = load_config()


def _names() -> list[str]:
    names = ["off", "def", "off_1h", "def_1h", "tempo", "tempo_1h", "elo"]
    for stat in FACTOR_STATS:
        names += [f"{stat}_o", f"{stat}_d"]
    return names


@dataclass
class Priors:
    """Preseason ratings by team, plus a value for teams with no previous season."""

    by_team: dict[str, dict[str, float]]
    newcomer: dict[str, float]
    league: dict[str, float]

    def value(self, name: str, team: str) -> float:
        row = self.by_team.get(team)
        return row[name] if row is not None else self.newcomer[name]


@dataclass
class RatingsSnapshot:
    season: int
    as_of: datetime
    games_visible: int
    team_ids: list[str]
    values: dict[str, np.ndarray]
    league: dict[str, float]
    played: np.ndarray
    k: float
    form: np.ndarray
    volatility: np.ndarray
    priors: Priors

    def _index(self, team: str) -> int | None:
        return self._positions.get(team)

    def __post_init__(self) -> None:
        self._positions = {team: i for i, team in enumerate(self.team_ids)}

    def value(self, name: str, team: str) -> float:
        position = self._index(team)
        if position is None:
            return self.priors.value(name, team)
        return float(self.values[name][position])

    def games_played(self, team: str) -> int:
        position = self._index(team)
        return 0 if position is None else int(self.played[position])

    def prior_weight(self, team: str) -> float:
        return self.k / (self.k + self.games_played(team))

    def form_of(self, team: str) -> float | None:
        position = self._index(team)
        if position is None or np.isnan(self.form[position]):
            return None
        return float(self.form[position])

    def volatility_of(self, team: str) -> float | None:
        position = self._index(team)
        if position is None or np.isnan(self.volatility[position]):
            return None
        return float(self.volatility[position])


def _elo_expected(diff: float) -> float:
    return float(1.0 / (1.0 + 10.0 ** (-diff / 400.0)))


def _elo_update(
    elo_home: float, elo_away: float, margin: float, neutral: bool, k: float, hfa: float
) -> float:
    """Points moving to the home team, with a FiveThirtyEight-style margin multiplier."""
    diff = elo_home - elo_away + (0.0 if neutral else hfa)
    won = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
    winner_diff = diff if margin >= 0 else -diff
    multiplier = (abs(margin) + 3.0) ** 0.8 / (7.5 + 0.006 * winner_diff)
    return float(k * multiplier * (won - _elo_expected(diff)))


class Engine:
    """Holds one History, and caches anything that depends only on complete past seasons."""

    def __init__(self, history: History) -> None:
        self.history = history
        self.config = CONFIG[history.league]
        self._priors: dict[int, Priors] = {}
        self._cache: dict[int, RatingsSnapshot] = {}
        self._finals: dict[int, RatingsSnapshot] = {}
        self._elo: dict[int, tuple[int, dict[str, float]]] = {}

    def ratings_as_of(self, season: int, as_of: datetime) -> RatingsSnapshot:
        # A snapshot depends only on which results are known, so reuse it until that changes.
        cached = self._cache.get(season)
        if cached is not None and cached.games_visible == self.history.season(season).count(as_of):
            return cached
        snapshot = self._solve(season, as_of)
        self._cache[season] = snapshot
        return snapshot

    def season_final(self, season: int) -> RatingsSnapshot:
        """A complete season with every game weighted equally (D52).

        Recency weighting is for predicting the next game. At season's end it would fade the
        whole regular season of any team that missed the playoffs, back toward its prior.
        """
        if season not in self._finals:
            self._finals[season] = self._solve(season, END_OF_TIME, recency=False)
        return self._finals[season]

    def priors_for(self, season: int) -> Priors:
        """Last season's final ratings, regressed toward the mean. Never this season's games."""
        if season not in self._priors:
            self._priors[season] = self._build_priors(season)
        return self._priors[season]

    def _opening_priors(self) -> Priors:
        newcomer = {name: 0.0 for name in _names()}
        newcomer["elo"] = ELO_BASE
        league = {
            "mu": float(self.config["mu"]),
            "tau": float(self.config["tau"]),
            "hca": float(self.config["hca"]),
            "mu_1h": float(self.config["mu"]),
            "tau_1h": float(self.config["tau"]) / 2.0,
            **{f"mu_{stat}": PRIOR_LEVELS[stat] for stat in FACTOR_STATS},
        }
        return Priors({}, newcomer, league)

    def _build_priors(self, season: int) -> Priors:
        previous = season - 1
        if previous not in self.history.seasons():
            return self._opening_priors()
        end = self.season_final(previous)
        keep = 1.0 - float(self.config["r"])
        by_team: dict[str, dict[str, float]] = {}
        for team in end.team_ids:
            row = {name: keep * end.value(name, team) for name in _names() if name != "elo"}
            row["elo"] = ELO_BASE + keep * (end.value("elo", team) - ELO_BASE)
            by_team[team] = row

        # Docs/04: teams new to the league start at the bottom quartile's mean.
        eligible = [team for team in by_team if team != NON_D1]
        ranked = sorted(eligible, key=lambda t: by_team[t]["off"] + by_team[t]["def"])
        bottom = ranked[: max(1, int(len(ranked) * BOTTOM_QUARTILE))]
        newcomer = {
            name: float(np.mean([by_team[team][name] for team in bottom])) for name in _names()
        }
        return Priors(by_team, newcomer, dict(end.league))

    def _solve(self, season: int, as_of: datetime, recency: bool = True) -> RatingsSnapshot:
        data = self.history.season(season)
        priors = self.priors_for(season)
        visible = data.count(as_of)
        k = float(self.config["k"])

        played_counts = np.bincount(
            np.concatenate([data.home[:visible], data.away[:visible]]), minlength=len(data.teams)
        )
        present = np.flatnonzero(played_counts > 0)
        team_ids = [data.teams[i] for i in present]
        slot = {int(team): position for position, team in enumerate(present)}

        values: dict[str, np.ndarray] = {
            name: np.array([priors.value(name, t) for t in team_ids]) for name in _names()
        }
        league = dict(priors.league)
        box = np.flatnonzero(data.has_box[:visible])
        if len(box):
            weights = 0.5 ** (
                (int(data.tips[visible - 1]) - data.tips[box])  # From the newest known game (D52).
                / MICROS_PER_DAY
                / float(self.config["half_life_days"])
            )
            if not recency:
                weights = np.ones(len(box))
            self._solve_od(data, box, weights, slot, team_ids, priors, k, values, league)
            self._solve_tempo(data, box, weights, slot, team_ids, priors, k, values, league)
        values["elo"] = self._elo_values(data, visible, team_ids, priors)
        form, volatility = self._form(data, visible, slot, values, league, len(team_ids))
        return RatingsSnapshot(
            season=season,
            as_of=as_of,
            games_visible=visible,
            team_ids=team_ids,
            values=values,
            league=league,
            played=played_counts[present],
            k=k,
            form=form,
            volatility=volatility,
            priors=priors,
        )

    def _solve_od(
        self,
        data: SeasonData,
        box: np.ndarray,
        weights: np.ndarray,
        slot: dict[int, int],
        team_ids: list[str],
        priors: Priors,
        k: float,
        values: dict[str, np.ndarray],
        league: dict[str, float],
    ) -> None:
        count = len(team_ids)
        size = 2 * count + 2  # O, D, mu, h.
        home = np.array([slot[int(t)] for t in data.home[box]])
        away = np.array([slot[int(t)] for t in data.away[box]])
        site = np.where(data.neutral[box], 0.0, 0.5)
        rows = np.arange(2 * len(box))
        offence = np.concatenate([home, away])
        defence = np.concatenate([away, home])
        sites = np.concatenate([site, -site])
        row_weights = np.concatenate([weights, weights])
        design = sp.csr_matrix(
            (
                np.concatenate(
                    [np.ones(len(rows)), -np.ones(len(rows)), np.ones(len(rows)), sites]
                ),
                (
                    np.concatenate([rows, rows, rows, rows]),
                    np.concatenate(
                        [offence, count + defence, np.full(len(rows), 2 * count),
                         np.full(len(rows), 2 * count + 1)]
                    ),
                ),
            ),
            shape=(len(rows), size),
        )  # fmt: skip
        # The home edge needs a real prior: early in a season good teams host weak ones, and
        # with few games per team a free term would absorb those blowouts (D55).
        penalties = np.concatenate(
            [np.full(2 * count, k), [LEVEL_PENALTY, float(self.config["hca_k"])]]
        )
        system = (design.T @ sp.diags(row_weights) @ design + sp.diags(penalties)).tocsc()
        solver = splu(system)

        hca_prior = league["hca"]
        for stat in OD_STATS:
            y = np.concatenate([data.stats_home[stat][box], data.stats_away[stat][box]])
            usable = np.isfinite(y)
            names = {
                "ppp": ("off", "def", "mu"),
                "ppp_1h": ("off_1h", "def_1h", "mu_1h"),
            }.get(stat, (f"{stat}_o", f"{stat}_d", f"mu_{stat}"))
            prior = np.concatenate(
                [
                    [priors.value(names[0], team) for team in team_ids],
                    [priors.value(names[1], team) for team in team_ids],
                    [league[names[2]], hca_prior if stat in ("ppp", "ppp_1h") else 0.0],
                ]
            )
            y_filled = np.where(usable, y, 0.0)
            used_weights = row_weights * usable
            right = design.T @ (used_weights * y_filled) + penalties * prior
            if not usable.all():
                solution = splu(
                    (design.T @ sp.diags(used_weights) @ design + sp.diags(penalties)).tocsc()
                ).solve(right)
            else:
                solution = solver.solve(right)
            values[names[0]] = solution[:count]
            values[names[1]] = solution[count : 2 * count]
            league[names[2]] = float(solution[2 * count])
            if stat == "ppp":
                league["hca"] = float(solution[2 * count + 1])

    def _solve_tempo(
        self,
        data: SeasonData,
        box: np.ndarray,
        weights: np.ndarray,
        slot: dict[int, int],
        team_ids: list[str],
        priors: Priors,
        k: float,
        values: dict[str, np.ndarray],
        league: dict[str, float],
    ) -> None:
        count = len(team_ids)
        home = np.array([slot[int(t)] for t in data.home[box]])
        away = np.array([slot[int(t)] for t in data.away[box]])
        rows = np.arange(len(box))
        design = sp.csr_matrix(
            (
                np.ones(3 * len(rows)),
                (
                    np.concatenate([rows, rows, rows]),
                    np.concatenate([home, away, np.full(len(rows), count)]),
                ),
            ),
            shape=(len(rows), count + 1),
        )
        penalties = np.concatenate([np.full(count, k), [LEVEL_PENALTY]])
        solver = splu((design.T @ sp.diags(weights) @ design + sp.diags(penalties)).tocsc())
        for name, level, source in (
            ("tempo", "tau", data.poss),
            ("tempo_1h", "tau_1h", data.poss_1h),
        ):
            prior = np.concatenate(
                [[priors.value(name, team) for team in team_ids], [league[level]]]
            )
            y = np.nan_to_num(source[box], nan=league[level])
            solution = solver.solve(design.T @ (weights * y) + penalties * prior)
            values[name] = solution[:count]
            league[level] = float(solution[count])

    def _elo_values(
        self, data: SeasonData, visible: int, team_ids: list[str], priors: Priors
    ) -> np.ndarray:
        """Replay results in order. Results are cached by how many games are visible."""
        start, state = self._elo.get(data.season, (0, {}))
        if start > visible:
            start, state = 0, {}
        state = dict(state)
        k = float(self.config["elo_k"])
        hfa = float(self.config["elo_hfa"])
        for position in range(start, visible):
            home = data.teams[data.home[position]]
            away = data.teams[data.away[position]]
            elo_home = state.get(home, priors.value("elo", home))
            elo_away = state.get(away, priors.value("elo", away))
            margin = float(data.home_pts[position] - data.away_pts[position])
            shift = _elo_update(elo_home, elo_away, margin, bool(data.neutral[position]), k, hfa)
            state[home] = elo_home + shift
            state[away] = elo_away - shift
        self._elo[data.season] = (visible, state)
        return np.array([state.get(team, priors.value("elo", team)) for team in team_ids])

    def _form(
        self,
        data: SeasonData,
        visible: int,
        slot: dict[int, int],
        values: dict[str, np.ndarray],
        league: dict[str, float],
        count: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Mean of each team's last 5 margin residuals, and the spread of all of them."""
        form = np.full(count, np.nan)
        volatility = np.full(count, np.nan)
        box = np.flatnonzero(data.has_box[:visible])
        if not len(box):
            return form, volatility
        home = np.array([slot[int(t)] for t in data.home[box]])
        away = np.array([slot[int(t)] for t in data.away[box]])
        edge = np.where(data.neutral[box], 0.0, league["hca"] / 2)
        expected_home = league["mu"] + values["off"][home] - values["def"][away] + edge
        expected_away = league["mu"] + values["off"][away] - values["def"][home] - edge
        scale = data.raw_poss[box] / 100
        expected = (expected_home - expected_away) * scale
        residual = (data.home_pts[box] - data.away_pts[box]) - expected
        for team, position in slot.items():
            mine = np.flatnonzero((data.home[box] == team) | (data.away[box] == team))
            if not len(mine):
                continue
            signs = np.where(data.home[box][mine] == team, 1.0, -1.0)
            team_residuals = residual[mine] * signs
            form[position] = float(team_residuals[-5:].mean())
            if len(team_residuals) >= 2:
                volatility[position] = float(team_residuals.std())
        return form, volatility
