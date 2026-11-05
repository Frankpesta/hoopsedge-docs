"""The one place that reads curated game tables for ratings and features.

Games are held sorted by tip time, so what's knowable at `as_of` is always a prefix: `count(as_of)`
games. Nothing later is ever handed to a calculation.

D14: a result counts only once `tip + 3 h < as_of`, stricter than invariant 1's `tip < as_of`, so
replays can't see games that wouldn't have finished. Live prediction uses the same rule.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from hoopsedge.ingest.curated import read_table
from hoopsedge.teams.registry import ratings_team_id

RESULT_KNOWN_AFTER = timedelta(hours=3)  # D14.
REGULATION_MINUTES = {"NBA": 48, "NCAA": 40}
OFFENCE_STATS = ("ppp", "ppp_1h", "efg", "tov", "orb", "ftr", "par")


def to_micros(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


@dataclass(frozen=True)
class GameRef:
    """What's known about a game before it tips. It holds no result."""

    league: str
    season: int
    game_id: str
    tip_utc: datetime
    home_id: str
    away_id: str
    neutral: bool
    postseason: bool
    slate_date_et: date


@dataclass
class SeasonData:
    """One season's games as arrays, sorted by tip. Team numbers follow sorted team IDs."""

    season: int
    teams: list[str]
    team_index: dict[str, int]
    game_ids: list[str]
    tips: np.ndarray
    home: np.ndarray
    away: np.ndarray
    neutral: np.ndarray
    home_pts: np.ndarray
    away_pts: np.ndarray
    has_box: np.ndarray
    poss: np.ndarray  # Scaled to a regulation game.
    poss_1h: np.ndarray
    raw_poss: np.ndarray
    stats_home: dict[str, np.ndarray]  # With the home team on offence.
    stats_away: dict[str, np.ndarray]
    games_by_team: list[np.ndarray]  # Positions in the sorted game list.

    def count(self, as_of: datetime) -> int:
        """How many games have a known result at `as_of` (D14). Strictly before, as a prefix."""
        cutoff = to_micros(as_of) - to_micros_delta(RESULT_KNOWN_AFTER)
        return int(np.searchsorted(self.tips, cutoff, side="left"))

    def played_before(self, team: str, moment: datetime) -> np.ndarray:
        """Tip times of a team's games before `moment`. These are schedule facts, not results."""
        index = self.team_index.get(team)
        if index is None:
            return np.empty(0, dtype=np.int64)
        tips: np.ndarray = self.tips[self.games_by_team[index]]
        return tips[: int(np.searchsorted(tips, to_micros(moment), side="left"))]


def to_micros_delta(delta: timedelta) -> int:
    return int(delta.total_seconds() * 1_000_000)


def _rate(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    result: np.ndarray = np.divide(top, bottom, out=np.full(len(top), np.nan), where=bottom > 0)
    return result


def _offence_stats(
    box: pl.DataFrame, opponent: pl.DataFrame, poss: np.ndarray, poss_1h: np.ndarray
) -> dict[str, np.ndarray]:
    def col(frame: pl.DataFrame, name: str) -> np.ndarray:
        return frame[name].to_numpy().astype(float)

    fga = col(box, "fga")
    return {
        "ppp": 100 * _rate(col(box, "pts"), col(box, "poss")),
        "ppp_1h": 100 * _rate(col(box, "pts_1h"), poss_1h),
        "efg": _rate(col(box, "fgm") + 0.5 * col(box, "fg3m"), fga),
        "tov": _rate(col(box, "tov"), col(box, "poss")),
        "orb": _rate(col(box, "oreb"), col(box, "oreb") + col(opponent, "dreb")),
        "ftr": _rate(col(box, "ftm"), fga),
        "par": _rate(col(box, "fg3a"), fga),
    }


class History:
    def __init__(
        self,
        league: str,
        games: pl.DataFrame,
        team_games: pl.DataFrame,
        player_games: pl.DataFrame,
        injuries: pl.DataFrame,
        d1_by_season: dict[int, set[str]],
    ) -> None:
        self.league = league
        self._d1 = d1_by_season
        self._seasons: dict[int, SeasonData] = {}
        self._games = games.sort(["tip_utc", "game_id"])
        self._team_games = team_games
        self._players = player_games
        self._injuries = injuries.sort("captured_at") if len(injuries) else injuries
        self._by_season_players: dict[int, pl.DataFrame] = {}

    @classmethod
    def load(cls, data_dir: Path, league: str, seasons: list[int]) -> "History":
        """Read one league's curated tables. D42: team IDs mean nothing without the league."""

        def table(name: str) -> pl.DataFrame:
            frames = [read_table(data_dir, name, league, season) for season in seasons]
            frames = [frame for frame in frames if len(frame)]
            return pl.concat(frames) if frames else pl.DataFrame()

        seasons_file = data_dir / "curated/team_seasons/team_seasons.parquet"
        d1: dict[int, set[str]] = {}
        if league == "NCAA" and seasons_file.exists():
            rows = pl.read_parquet(seasons_file).filter(
                (pl.col("league") == "NCAA") & pl.col("is_d1")
            )
            for season, frame in rows.group_by("season"):
                d1[int(season[0])] = set(frame["team_id"].to_list())
        injury_files = sorted((data_dir / "curated/injuries/league=NBA").rglob("part-*.parquet"))
        injuries = (
            pl.concat([pl.read_parquet(file) for file in injury_files])
            if league == "NBA" and injury_files
            else pl.DataFrame()
        )
        return cls(league, table("games"), table("team_games"), table("player_games"), injuries, d1)

    def seasons(self) -> list[int]:
        return sorted(self._games["season"].unique().to_list()) if len(self._games) else []

    def d1_teams(self, season: int) -> set[str] | None:
        return self._d1.get(season)

    def pooled_team(self, season: int, team_id: str) -> str:
        return ratings_team_id(self.league, team_id, self._d1.get(season))

    def season(self, season: int) -> SeasonData:
        if season not in self._seasons:
            self._seasons[season] = self._build_season(season)
        return self._seasons[season]

    def games_frame(self, season: int) -> pl.DataFrame:
        """All of a season's rows, for replay and for targets. Never for features."""
        return self._games.filter(pl.col("season") == season)

    def _build_season(self, season: int) -> SeasonData:
        games = self.games_frame(season)
        d1 = self._d1.get(season)
        home_ids = [ratings_team_id(self.league, t, d1) for t in games["home_id"].to_list()]
        away_ids = [ratings_team_id(self.league, t, d1) for t in games["away_id"].to_list()]
        teams = sorted(set(home_ids) | set(away_ids))
        index = {team: number for number, team in enumerate(teams)}
        home = np.array([index[t] for t in home_ids], dtype=np.int64)
        away = np.array([index[t] for t in away_ids], dtype=np.int64)
        count = len(games)
        game_ids = games["game_id"].to_list()

        boxed = self._boxes(games)
        has_box = np.zeros(count, dtype=bool)
        raw_poss = np.full(count, np.nan)
        poss = np.full(count, np.nan)
        poss_1h = np.full(count, np.nan)
        empty = np.full(count, np.nan)
        stats_home = {name: empty.copy() for name in OFFENCE_STATS}
        stats_away = {name: empty.copy() for name in OFFENCE_STATS}
        if len(boxed):
            positions = boxed["position"].to_numpy()
            has_box[positions] = True
            home_total = boxed["home_pts_game"].to_numpy().astype(float)
            away_total = boxed["away_pts_game"].to_numpy().astype(float)
            half_total = boxed["home_1h_game"].to_numpy().astype(float) + boxed[
                "away_1h_game"
            ].to_numpy().astype(float)
            box_poss = boxed["poss"].to_numpy().astype(float)
            # D51: the share is the game's, so first-half efficiency differs from full-game.
            half_poss = box_poss * _rate(half_total, home_total + away_total)
            regulation = REGULATION_MINUTES[self.league]
            raw_poss[positions] = box_poss
            poss[positions] = box_poss * regulation / boxed["minutes"].to_numpy().astype(float)
            poss_1h[positions] = half_poss
            home_box = boxed.select([c for c in boxed.columns if not c.endswith("_opp")])
            away_box = boxed.select(
                [
                    pl.col(c).alias(c.removesuffix("_opp"))
                    for c in boxed.columns
                    if c.endswith("_opp")
                ]
                + ["poss", "minutes"]
            )
            for target, stats in (
                (stats_home, _offence_stats(home_box, away_box, box_poss, half_poss)),
                (stats_away, _offence_stats(away_box, home_box, box_poss, half_poss)),
            ):
                for name, values in stats.items():
                    target[name][positions] = values

        by_team: list[list[int]] = [[] for _ in teams]
        for position in range(count):
            by_team[home[position]].append(position)
            by_team[away[position]].append(position)
        return SeasonData(
            season=season,
            teams=teams,
            team_index=index,
            game_ids=game_ids,
            tips=games["tip_utc"].dt.timestamp("us").to_numpy(),
            home=home,
            away=away,
            neutral=games["neutral"].to_numpy(),
            home_pts=games["home_pts"].to_numpy().astype(float),
            away_pts=games["away_pts"].to_numpy().astype(float),
            has_box=has_box,
            poss=poss,
            poss_1h=poss_1h,
            raw_poss=raw_poss,
            stats_home=stats_home,
            stats_away=stats_away,
            games_by_team=[np.array(positions, dtype=np.int64) for positions in by_team],
        )

    def _boxes(self, games: pl.DataFrame) -> pl.DataFrame:
        """One row per game with a usable box: home columns, and away columns ending `_opp`."""
        if not len(self._team_games):
            return pl.DataFrame()
        keyed = games.select(
            pl.col("game_id"),
            pl.col("box_missing"),
            pl.col("home_pts").alias("home_pts_game"),
            pl.col("away_pts").alias("away_pts_game"),
            pl.col("home_1h").alias("home_1h_game"),
            pl.col("away_1h").alias("away_1h_game"),
        ).with_row_index("position")
        keyed = keyed.filter(~pl.col("box_missing")).drop("box_missing")
        box = self._team_games.select(
            "game_id", "is_home", "fgm", "fga", "fg3m", "fg3a", "ftm", "oreb", "dreb",
            "tov", "pts", "pts_1h", "minutes", "poss",
        )  # fmt: skip
        home = box.filter(pl.col("is_home")).drop("is_home")
        away = box.filter(~pl.col("is_home")).drop(["is_home", "minutes", "poss"])
        away = away.rename({c: f"{c}_opp" for c in away.columns if c != "game_id"})
        return keyed.join(home, on="game_id").join(away, on="game_id").sort("position")

    def latest_injuries(self, as_of: datetime, max_age: timedelta) -> pl.DataFrame | None:
        """The newest snapshot strictly before `as_of`, unless too old. None means unknown."""
        if not len(self._injuries):
            return None
        earlier = self._injuries.filter(pl.col("captured_at") < as_of)
        if not len(earlier):
            return None
        latest = earlier["captured_at"].max()
        if as_of - latest > max_age:  # type: ignore[operator]
            return None
        return earlier.filter(pl.col("captured_at") == latest)

    def player_minutes(self, season: int, team_id: str, as_of: datetime) -> pl.DataFrame:
        """Each player's minutes and starts for this team, from games with known results."""
        if season not in self._by_season_players:
            games = self.games_frame(season).select("game_id", "tip_utc")
            self._by_season_players[season] = (
                self._players.join(games, on="game_id") if len(self._players) else pl.DataFrame()
            )
        rows = self._by_season_players[season]
        if not len(rows):
            return rows
        return (
            rows.filter(
                (pl.col("team_id") == team_id) & (pl.col("tip_utc") + RESULT_KNOWN_AFTER < as_of)
            )
            .group_by("player_id")
            .agg(pl.col("minutes").sum(), pl.col("starter").mean().alias("start_rate"))
        )
