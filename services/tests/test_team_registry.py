"""Team registry and aliases (docs/03 section 2.5, invariant 5)."""

import json
from pathlib import Path
from typing import Any

import pytest

from hoopsedge.teams.normalize import normalize_name
from hoopsedge.teams.registry import (
    NON_D1,
    TeamSighting,
    build_aliases,
    d1_ids_from_core,
    load_hard_cases,
    ratings_team_id,
    teams_from_list,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


NCAA = teams_from_list("NCAA", load("teams-ncaa.json"))
D1 = d1_ids_from_core(load("d1-teams-2027.json"))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Michigan State", "michigan st"),
        ("Michigan St.", "michigan st"),
        ("Saint Mary's", "st marys"),
        ("St. Mary's", "st marys"),
        ("Texas A&M", "texas a and m"),
        ("Texas A and M", "texas a and m"),
        ("Miami (OH)", "miami oh"),
        ("San José State", "san jose st"),
        ("Hawai'i", "hawaii"),
        ("Boston University", "boston"),
        ("Seattle U", "seattle"),
        ("Univ. of Kansas", "of kansas"),
        ("  UConn  ", "uconn"),
        ("Arkansas-Pine Bluff", "arkansas pine bluff"),
    ],
)
def test_normalize(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


def test_real_lists_have_the_expected_sizes() -> None:
    assert len(teams_from_list("NBA", load("teams-nba.json"))) == 30
    assert len(NCAA) == 362
    assert len(D1) == load("d1-teams-2027.json")["count"]
    assert {sighting.team_id for sighting in NCAA} <= D1


def test_every_d1_team_in_the_list_has_at_least_three_aliases() -> None:
    # These names have no genuine third form, and an invented alias adds no matching power (D41).
    no_third_form = {
        "Duke Blue Devils",
        "Elon Phoenix",
        "Ohio Bobcats",
        "Rice Owls",
        "Yale Bulldogs",
    }
    aliases, _ = build_aliases("NCAA", NCAA, load_hard_cases()["NCAA"])
    counts: dict[str, int] = {}
    for alias in aliases:
        counts[alias.team_id] = counts.get(alias.team_id, 0) + 1
    thin = {s.full_name: counts.get(s.team_id, 0) for s in NCAA if counts.get(s.team_id, 0) < 3}
    assert set(thin) == no_third_form
    assert set(thin.values()) == {2}


def test_an_alias_shared_by_two_teams_goes_to_neither() -> None:
    sightings = [
        TeamSighting("NCAA", "1", "Loyola Ramblers", "Loyola", "LOY", "Loyola", "Ramblers"),
        TeamSighting("NCAA", "2", "Loyola Greyhounds", "Loyola", "LMD", "Loyola", "Greyhounds"),
    ]
    aliases, ambiguous = build_aliases("NCAA", sightings, {})
    assert "loyola" not in {alias.alias_normalized for alias in aliases}
    assert ambiguous["loyola"] == ["1", "2"]


def by_name(full_name: str) -> str:
    return next(s.team_id for s in NCAA if s.full_name == full_name)


@pytest.mark.parametrize(
    ("alias", "full_name"),
    [
        ("Miami FL", "Miami Hurricanes"),
        ("Miami (Florida)", "Miami Hurricanes"),
        ("Miami OH", "Miami (OH) RedHawks"),
        ("St. John's (NY)", "St. John's Red Storm"),
        ("Saint Mary's (CA)", "Saint Mary's Gaels"),
        ("Loyola Chicago", "Loyola Chicago Ramblers"),
        ("Loyola (MD)", "Loyola Maryland Greyhounds"),
        ("LMU", "Loyola Marymount Lions"),
        ("Southern California", "USC Trojans"),
        ("Connecticut", "UConn Huskies"),
        ("NC Asheville", "UNC Asheville Bulldogs"),
        ("NC State", "NC State Wolfpack"),
        ("North Carolina State", "NC State Wolfpack"),
        ("Texas A&M Commerce", "East Texas A&M Lions"),
        ("Texas A&M CC", "Texas A&M-Corpus Christi Islanders"),
        ("LIU", "Long Island University Sharks"),
        ("McNeese State", "McNeese Cowboys"),
        ("Brigham Young", "BYU Cougars"),
        ("Louisiana State", "LSU Tigers"),
        ("Central Florida", "UCF Knights"),
        ("Virginia Commonwealth", "VCU Rams"),
    ],
)
def test_hard_cases_resolve_to_the_right_team(alias: str, full_name: str) -> None:
    aliases, _ = build_aliases("NCAA", NCAA, load_hard_cases()["NCAA"])
    table = {row.alias_normalized: row.team_id for row in aliases}
    assert table[normalize_name(alias)] == by_name(full_name)


@pytest.mark.parametrize(
    ("alias", "wrong"),
    [
        ("South Carolina", "USC Trojans"),
        ("Miami (OH)", "Miami Hurricanes"),
        ("Loyola Marymount", "Loyola Chicago Ramblers"),
        ("UNC Wilmington", "North Carolina Tar Heels"),
        ("Texas A&M", "East Texas A&M Lions"),
    ],
)
def test_lookalike_teams_do_not_cross(alias: str, wrong: str) -> None:
    aliases, _ = build_aliases("NCAA", NCAA, load_hard_cases()["NCAA"])
    table = {row.alias_normalized: row.team_id for row in aliases}
    assert table.get(normalize_name(alias)) != by_name(wrong)


def test_a_hard_case_for_an_unknown_team_fails_loudly() -> None:
    with pytest.raises(ValueError, match="Nowhere State"):
        build_aliases("NCAA", NCAA, {"Nowhere State": ["NSU"]})


def test_non_d1_teams_pool_for_ratings_only_in_ncaa() -> None:
    assert ratings_team_id("NCAA", "126", {"2", "61"}) == NON_D1
    assert ratings_team_id("NCAA", "61", {"2", "61"}) == "61"
    assert ratings_team_id("NBA", "18", None) == "18"
