"""Canonical teams and aliases (docs/03-data-ingestion.md section 2.5, invariant 5)."""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hoopsedge.teams.normalize import normalize_name

NON_D1 = "NON_D1"
_CORE_TEAM_ID = re.compile(r"/teams/(\d+)")
_SOURCE_ESPN = "espn"
_SOURCE_CURATED = "curated"


@dataclass(frozen=True)
class TeamSighting:
    league: str
    team_id: str
    full_name: str
    short_name: str
    abbreviation: str
    location: str
    nickname: str
    mascot: str = ""


@dataclass(frozen=True)
class Alias:
    league: str
    alias_normalized: str
    team_id: str
    source: str


def sighting_from_team(league: str, team: Mapping[str, Any]) -> TeamSighting:
    return TeamSighting(
        league=league,
        team_id=str(team["id"]),
        full_name=str(team.get("displayName") or ""),
        short_name=str(team.get("shortDisplayName") or ""),
        abbreviation=str(team.get("abbreviation") or ""),
        location=str(team.get("location") or ""),
        nickname=str(team.get("nickname") or ""),
        mascot=str(team.get("name") or ""),
    )


def teams_from_list(league: str, document: Mapping[str, Any]) -> list[TeamSighting]:
    """Read ESPN's site teams endpoint."""
    entries = document["sports"][0]["leagues"][0]["teams"]
    return [sighting_from_team(league, entry["team"]) for entry in entries]


def d1_ids_from_core(document: Mapping[str, Any]) -> set[str]:
    """Read ESPN's per-season Division I list, whose items are links holding the team ID."""
    ids = set()
    for item in document["items"]:
        found = _CORE_TEAM_ID.search(str(item["$ref"]))
        if found is None:
            raise ValueError(f"Can't read a team ID from {item['$ref']}")
        ids.add(found.group(1))
    return ids


def load_hard_cases() -> dict[str, dict[str, list[str]]]:
    path = Path(__file__).with_name("hard_cases.yaml")
    cases: dict[str, dict[str, list[str]]] = yaml.safe_load(path.read_text("utf-8"))
    return cases


def build_aliases(
    league: str,
    sightings: Sequence[TeamSighting],
    hard_cases: Mapping[str, Iterable[str]],
) -> tuple[list[Alias], dict[str, list[str]]]:
    """Return usable aliases, and aliases that reached two teams and so can't be used.

    A hard case outranks an ESPN name, because it was added to settle that name.
    """
    by_display = {sighting.full_name: sighting.team_id for sighting in sightings}
    curated: dict[str, str] = {}
    for full_name, names in hard_cases.items():
        if full_name not in by_display:
            raise ValueError(f"Hard case for unknown team: {full_name}")
        for name in names:
            curated[normalize_name(name)] = by_display[full_name]

    espn: dict[str, set[str]] = {}
    for sighting in sightings:
        names = (
            sighting.full_name,
            sighting.short_name,
            sighting.abbreviation,
            sighting.location,
            sighting.nickname,
            sighting.mascot,  # Usable only when one team has it. Shared ones go to `ambiguous`.
        )
        for name in names:
            normalized = normalize_name(name)
            if normalized:
                espn.setdefault(normalized, set()).add(sighting.team_id)

    aliases = [
        Alias(league, alias, team_id, _SOURCE_CURATED) for alias, team_id in sorted(curated.items())
    ]
    ambiguous: dict[str, list[str]] = {}
    for alias, team_ids in sorted(espn.items()):
        if alias in curated:
            continue
        if len(team_ids) == 1:
            aliases.append(Alias(league, alias, next(iter(team_ids)), _SOURCE_ESPN))
        else:
            ambiguous[alias] = sorted(team_ids)
    return aliases, ambiguous


def ratings_team_id(league: str, team_id: str, d1_ids: Collection[str] | None) -> str:
    """Non-D1 opponents share one pooled team for ratings. NBA has no such pool."""
    if league == "NCAA" and d1_ids is not None and team_id not in d1_ids:
        return NON_D1
    return team_id
