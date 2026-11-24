"""SportyBet team names to ESPN team IDs, and events to ESPN games (brief §5.3 as amended by D89).

Order: the sport's alias table, then the exact ESPN display name, then location plus mascot with
exactly one candidate. Nothing else matches. Fuzzy scores only suggest teams to the review
queue; there is no first-word, substring or fuzzy match (brief invariant 5). Then the pairing must
be an ESPN game whose kickoff is within 3 hours of SportyBet's.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

ALIASES_FILE = Path(__file__).with_name("aliases.yaml")
MAX_KICKOFF_GAP = timedelta(hours=3)
SUGGESTIONS = 3
NORMAL = "normal"
SWAPPED = "swapped"
_MOJIBAKE = re.compile("[ÂÃ]")
_APOSTROPHES = re.compile("['’`´]")
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _unmangle(text: str) -> str:
    """UTF-8 read as Latin-1, as in SportyBet's 'FightinÂ´'. Left alone if that isn't it."""
    if not _MOJIBAKE.search(text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def normalize(name: str) -> str:
    """Lowercase, no accents or punctuation, and `st` spelled out: Saint as the first word,
    State anywhere else (D89). Both sources go through this, so they agree whatever they write.
    """
    text = unicodedata.normalize("NFKD", _unmangle(name))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    words = _NOT_WORD.sub(" ", _APOSTROPHES.sub("", text)).split()
    return " ".join(
        ("saint" if index == 0 else "state") if word == "st" else word
        for index, word in enumerate(words)
    )


@dataclass(frozen=True)
class Team:
    team_id: str
    display_name: str
    short_name: str
    location: str
    mascot: str
    level: str


@dataclass(frozen=True)
class Resolution:
    team_id: str | None
    method: str | None  # alias, exact or location_mascot.
    reason: str | None  # Why not: unmatched or ambiguous.
    suggestions: tuple[tuple[str, float], ...] = ()  # (team ID, score), for the queue only.


@dataclass(frozen=True)
class EspnGame:
    game_id: str
    home_id: str
    away_id: str
    kickoff_utc: datetime
    neutral: bool


@dataclass(frozen=True)
class EventMatch:
    game: EspnGame | None
    orientation: str | None  # NORMAL when SportyBet's home is ESPN's home.
    reason: str | None
    home: Resolution
    away: Resolution

    @property
    def matched(self) -> bool:
        return self.game is not None


def _words(text: str) -> list[str]:
    return text.split()


def _contains(words: list[str], part: list[str]) -> bool:
    size = len(part)
    return size > 0 and any(words[i : i + size] == part for i in range(len(words) - size + 1))


class Directory:
    """One sport's ESPN teams and aliases."""

    def __init__(self, teams: Iterable[Team], aliases: Mapping[str, str]) -> None:
        self.teams = {team.team_id: team for team in teams}
        self.aliases: dict[str, str] = {}
        for alias, team_id in aliases.items():
            if team_id not in self.teams:
                raise ValueError(f"Alias {alias!r} points at unknown ESPN team {team_id}")
            self.aliases[normalize(alias)] = team_id
        self._display: dict[str, list[str]] = {}
        self._parts: list[tuple[list[str], list[str], str]] = []
        for team in self.teams.values():
            display = normalize(team.display_name)
            self._display.setdefault(display, []).append(team.team_id)
            location = normalize(team.location)
            mascot = (
                display[len(location) :].strip()
                if location and display.startswith(location + " ")
                else normalize(team.mascot)
            )
            if location and mascot:
                self._parts.append((_words(location), _words(mascot), team.team_id))

    def resolve(self, name: str) -> Resolution:
        normalized = normalize(name)
        if normalized in self.aliases:
            return Resolution(self.aliases[normalized], "alias", None)
        exact = self._display.get(normalized, [])
        if len(exact) == 1:
            return Resolution(exact[0], "exact", None)
        if len(exact) > 1:
            return Resolution(None, None, "ambiguous", self._suggest(normalized))
        words = _words(normalized)
        found = sorted(
            {
                team_id
                for location, mascot, team_id in self._parts
                if words[-len(mascot) :] == mascot and _contains(words[: -len(mascot)], location)
            }
        )
        if len(found) == 1:
            return Resolution(found[0], "location_mascot", None)
        reason = "ambiguous" if found else "unmatched"
        return Resolution(None, None, reason, self._suggest(normalized))

    def _suggest(self, normalized: str) -> tuple[tuple[str, float], ...]:
        scored = sorted(
            (
                (-fuzz.token_set_ratio(normalized, normalize(team.display_name)), team.team_id)
                for team in self.teams.values()
            ),
        )
        return tuple((team_id, -score) for score, team_id in scored[:SUGGESTIONS])


def match_event(
    home_name: str,
    away_name: str,
    start: datetime,
    games: Sequence[EspnGame],
    directory: Directory,
) -> EventMatch:
    home, away = directory.resolve(home_name), directory.resolve(away_name)

    def unmatched(reason: str) -> EventMatch:
        return EventMatch(None, None, reason, home, away)

    if home.team_id is None or away.team_id is None:
        return unmatched("team_not_resolved")
    if home.team_id == away.team_id:
        return unmatched("same_team_twice")
    pair = {home.team_id, away.team_id}
    found = [
        game
        for game in games
        if {game.home_id, game.away_id} == pair and abs(game.kickoff_utc - start) <= MAX_KICKOFF_GAP
    ]
    if not found:
        return unmatched("no_espn_game_within_3_hours")
    if len(found) > 1:
        return unmatched("several_espn_games")
    game = found[0]
    if game.home_id == home.team_id:
        return EventMatch(game, NORMAL, None, home, away)
    if not game.neutral:
        # A true home game listed the other way round is likelier a data error than a label.
        return unmatched("swapped_at_a_true_home_game")
    return EventMatch(game, SWAPPED, None, home, away)


def seed_aliases(sport: str) -> dict[str, str]:
    """The brief's seeds that belong to this sport, keyed by ESPN team ID (D89)."""
    document: dict[str, Any] = yaml.safe_load(ALIASES_FILE.read_text("utf-8"))
    return {str(name): str(team_id) for name, team_id in (document.get(sport) or {}).items()}
