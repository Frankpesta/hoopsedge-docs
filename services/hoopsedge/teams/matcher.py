"""SportyBet events to canonical games (docs/07 section 6.3, invariant 5).

A match needs certainty at every step. Anything else goes to the Unmatched queue with suggestions
and is excluded, never guessed. Fuzzy matching compares only aliases of two or more words: a lone
"Kansas" would otherwise match "Kansas Christian Falcons" perfectly, since one is inside the other.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
from rapidfuzz import fuzz

from hoopsedge.teams.normalize import normalize_name
from hoopsedge.teams.registry import load_hard_cases

MIN_SCORE = 92.0
MIN_MARGIN = 5.0
MAX_TIP_GAP = timedelta(hours=3)
SUGGESTIONS = 3
NORMAL = "normal"
SWAPPED = "swapped"


@dataclass(frozen=True)
class Suggestion:
    team_id: str
    score: float


@dataclass(frozen=True)
class Resolution:
    team_id: str | None
    method: str | None  # `alias` or `fuzzy`.
    suggestions: tuple[Suggestion, ...]


@dataclass(frozen=True)
class CanonicalGame:
    game_id: str
    home_id: str
    away_id: str
    tip_utc: datetime
    neutral: bool | None
    slate_date_et: date


@dataclass(frozen=True)
class Match:
    game_id: str | None
    orientation: str | None
    reason: str | None  # Why it isn't matched.
    home: Resolution
    away: Resolution

    @property
    def matched(self) -> bool:
        return self.game_id is not None


class TeamIndex:
    def __init__(self, aliases: Mapping[str, str]) -> None:
        self._aliases = dict(aliases)
        self._fuzzy = sorted(
            (alias, team_id) for alias, team_id in aliases.items() if len(alias.split()) >= 2
        )

    @classmethod
    def load(
        cls, data_dir: Path, league: str, confirmed: Mapping[str, str] | None = None
    ) -> "TeamIndex":
        """The curated aliases, plus hard cases added since, plus the owner's confirmations."""
        rows = pl.read_parquet(data_dir / "curated/team_aliases/team_aliases.parquet")
        chosen = rows.filter(pl.col("league") == league)
        aliases = dict(zip(chosen["alias_normalized"], chosen["team_id"], strict=True))
        teams = pl.read_parquet(data_dir / "curated/teams/teams.parquet").filter(
            pl.col("league") == league
        )
        by_name = dict(zip(teams["full_name"], teams["team_id"], strict=True))
        for full_name, names in load_hard_cases().get(league, {}).items():
            if full_name not in by_name:
                raise ValueError(f"Hard case for unknown team: {full_name}")
            for name in names:
                aliases[normalize_name(name)] = by_name[full_name]
        return cls({**aliases, **(confirmed or {})})

    def _ranked(self, name: str) -> list[Suggestion]:
        best: dict[str, float] = {}
        for alias, team_id in self._fuzzy:
            score = float(fuzz.token_set_ratio(name, alias))
            best[team_id] = max(best.get(team_id, 0.0), score)
        # Team ID breaks ties, so the suggestions are repeatable.
        ranked = sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))
        return [Suggestion(team_id, score) for team_id, score in ranked]

    def resolve(self, name: str) -> Resolution:
        normalized = normalize_name(name)
        team_id = self._aliases.get(normalized)
        if team_id is not None:
            return Resolution(team_id, "alias", ())
        ranked = self._ranked(normalized)
        suggestions = tuple(ranked[:SUGGESTIONS])
        if not ranked or ranked[0].score < MIN_SCORE:
            return Resolution(None, None, suggestions)
        margin = ranked[0].score - (ranked[1].score if len(ranked) > 1 else 0.0)
        if margin < MIN_MARGIN:
            return Resolution(None, None, suggestions)
        return Resolution(ranked[0].team_id, "fuzzy", ())


def match_event(
    home_name: str,
    away_name: str,
    start: datetime,
    games: Sequence[CanonicalGame],
    index: TeamIndex,
) -> Match:
    home = index.resolve(home_name)
    away = index.resolve(away_name)

    def unmatched(reason: str) -> Match:
        return Match(None, None, reason, home, away)

    if home.team_id is None or away.team_id is None:
        return unmatched("team_not_resolved")
    if home.team_id == away.team_id:
        return unmatched("same_team_twice")
    pair = {home.team_id, away.team_id}
    found = [
        game
        for game in games
        if {game.home_id, game.away_id} == pair and abs(game.tip_utc - start) <= MAX_TIP_GAP
    ]
    if not found:
        return unmatched("no_game_found")
    if len(found) > 1:
        return unmatched("several_games_found")
    game = found[0]
    if game.home_id == home.team_id:
        return Match(game.game_id, NORMAL, None, home, away)
    if game.neutral is False:
        # A true home game listed the wrong way round is more likely a data error than a label.
        return unmatched("swapped_at_a_true_home_game")
    return Match(game.game_id, SWAPPED, None, home, away)


def confirmed_aliases(rows: Iterable[Mapping[str, str]]) -> dict[str, str]:
    return {row["alias_normalized"]: row["team_id"] for row in rows}
