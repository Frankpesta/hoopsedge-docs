"""Team matching (docs/07 section 6.3, invariant 5): tricky names, and pairs that must not match."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from hoopsedge.teams.matcher import (
    NORMAL,
    SWAPPED,
    CanonicalGame,
    TeamIndex,
    confirmed_aliases,
    match_event,
)
from hoopsedge.teams.registry import build_aliases, load_hard_cases, teams_from_list

FIXTURE = Path(__file__).parent / "fixtures/espn/teams-ncaa.json"
START = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def registry() -> tuple[TeamIndex, dict[str, str]]:
    sightings = teams_from_list("NCAA", json.loads(FIXTURE.read_text("utf-8")))
    names = {sighting.full_name for sighting in sightings}
    hard = {key: value for key, value in load_hard_cases()["NCAA"].items() if key in names}
    aliases, _ = build_aliases("NCAA", sightings, hard)
    return (
        TeamIndex({alias.alias_normalized: alias.team_id for alias in aliases}),
        {sighting.full_name: sighting.team_id for sighting in sightings},
    )


MUST_MATCH = [
    # SportyBet's real spellings, from the collector.
    ("Central Connecticut State Blue Devils", "Central Connecticut Blue Devils"),
    ("FIU Panthers", "Florida International Panthers"),
    ("Illinois Chicago Flames", "UIC Flames"),
    ("Louisiana Lafayette Ragin Cajuns", "Louisiana Ragin' Cajuns"),
    ("Middle Tennessee State Blue Raiders", "Middle Tennessee Blue Raiders"),
    ("Nicholls State Colonels", "Nicholls Colonels"),
    ("North Carolina Greensboro Spartans", "UNC Greensboro Spartans"),
    ("Penn Quakers", "Pennsylvania Quakers"),
    ("SC Upstate Spartans", "South Carolina Upstate Spartans"),
    ("St. Thomas (MN) Tommies", "St. Thomas Tommies"),
    ("Texas AM Aggies", "Texas A&M Aggies"),
    ("Texas Arlington Mavericks", "UT Arlington Mavericks"),
    ("Tulane Green Waves", "Tulane Green Wave"),
    ("University At Albany Great Danes", "UAlbany Great Danes"),
    ("Wisconsin Green Bay Phoenix", "Green Bay Phoenix"),
    # Punctuation, accents and spacing.
    ("Duke Blue Devils", "Duke Blue Devils"),
    ("DUKE BLUE DEVILS", "Duke Blue Devils"),
    ("Hawai'i Rainbow Warriors", "Hawai'i Rainbow Warriors"),
    ("Hawaii Rainbow Warriors", "Hawai'i Rainbow Warriors"),
    ("San Jose State Spartans", "San José State Spartans"),
    ("Saint Mary's Gaels", "Saint Mary's Gaels"),
    ("St. Mary's CA", "Saint Mary's Gaels"),
    ("St. John's Red Storm", "St. John's Red Storm"),
    ("Saint John's", "St. John's Red Storm"),
    ("Saint Joseph's Hawks", "Saint Joseph's Hawks"),
    ("St Josephs PA", "Saint Joseph's Hawks"),
    ("Mount Saint Mary's", "Mount St. Mary's Mountaineers"),
    ("Texas A&M", "Texas A&M Aggies"),
    ("Texas A&M Corpus Christi", "Texas A&M-Corpus Christi Islanders"),
    # Names that differ between sources.
    ("Miami (FL)", "Miami Hurricanes"),
    ("Miami FL", "Miami Hurricanes"),
    ("Miami (Ohio)", "Miami (OH) RedHawks"),
    ("Miami OH", "Miami (OH) RedHawks"),
    ("Loyola Chicago", "Loyola Chicago Ramblers"),
    ("Loyola (IL)", "Loyola Chicago Ramblers"),
    ("Loyola Maryland", "Loyola Maryland Greyhounds"),
    ("Loyola (MD)", "Loyola Maryland Greyhounds"),
    ("Loyola Marymount", "Loyola Marymount Lions"),
    ("LMU", "Loyola Marymount Lions"),
    ("Southern California", "USC Trojans"),
    ("Connecticut", "UConn Huskies"),
    ("North Carolina", "North Carolina Tar Heels"),
    ("NC Asheville", "UNC Asheville Bulldogs"),
    ("NC Wilmington", "UNC Wilmington Seahawks"),
    ("NC A&T", "North Carolina A&T Aggies"),
    ("North Carolina State", "NC State Wolfpack"),
    ("Long Island", "Long Island University Sharks"),
    ("McNeese State", "McNeese Cowboys"),
    ("Brigham Young", "BYU Cougars"),
    ("Louisiana State", "LSU Tigers"),
    ("Central Florida", "UCF Knights"),
    ("Nevada Las Vegas", "UNLV Rebels"),
    ("Texas San Antonio", "UTSA Roadrunners"),
    ("Virginia Commonwealth", "VCU Rams"),
]

# Each is a real different team, a shared word, or not a D1 team. None may resolve to the second.
MUST_NOT_MATCH = [
    ("Penn State Nittany Lions", "Pennsylvania Quakers"),
    ("Pennsylvania Quakers", "Penn State Nittany Lions"),
    ("Miami (OH) RedHawks", "Miami Hurricanes"),
    ("Miami Hurricanes", "Miami (OH) RedHawks"),
    ("Texas A&M-Corpus Christi Islanders", "Texas A&M Aggies"),
    ("East Texas A&M Lions", "Texas A&M Aggies"),
    ("Tennessee State Tigers", "Tennessee Volunteers"),
    ("East Tennessee State Buccaneers", "Tennessee State Tigers"),
    ("Middle Tennessee Blue Raiders", "Tennessee Volunteers"),
    ("UNC Greensboro Spartans", "North Carolina Tar Heels"),
    ("North Carolina A&T Aggies", "North Carolina Tar Heels"),
    ("Loyola Chicago Ramblers", "Loyola Maryland Greyhounds"),
    ("Loyola Marymount Lions", "Loyola Chicago Ramblers"),
    ("Saint Joseph's Hawks", "Saint Mary's Gaels"),
    ("St. John's Red Storm", "Saint Joseph's Hawks"),
    ("Kansas State Wildcats", "Kansas Jayhawks"),
    ("Kansas City Roos", "Kansas Jayhawks"),
    ("Michigan State Spartans", "Michigan Wolverines"),
    ("Eastern Michigan Eagles", "Michigan Wolverines"),
    ("Southern Illinois Salukis", "Illinois Fighting Illini"),
    ("UIC Flames", "Illinois Fighting Illini"),
    ("Kentucky Christian Knights", "Kentucky Wildcats"),
    ("Kansas Christian Falcons", "Kansas Jayhawks"),
    ("Virginia Tech Hokies", "Virginia Cavaliers"),
    ("West Virginia Mountaineers", "Virginia Cavaliers"),
    ("Georgia Tech Yellow Jackets", "Georgia Bulldogs"),
    ("Georgia State Panthers", "Georgia Bulldogs"),
    ("Georgia Southern Eagles", "Georgia Bulldogs"),
    ("South Florida Bulls", "Florida Gators"),
    ("Florida State Seminoles", "Florida Gators"),
    ("Florida Atlantic Owls", "Florida Gators"),
]


def _covering_at_least_sixty() -> int:
    return len(MUST_MATCH) + len(MUST_NOT_MATCH)


def test_the_table_has_at_least_sixty_tricky_pairs() -> None:
    assert _covering_at_least_sixty() >= 60
    assert len(MUST_NOT_MATCH) >= 20


@pytest.mark.parametrize(("name", "team"), MUST_MATCH)
def test_tricky_names_reach_the_right_team(
    registry: tuple[TeamIndex, dict[str, str]], name: str, team: str
) -> None:
    index, teams = registry
    assert index.resolve(name).team_id == teams[team]


@pytest.mark.parametrize(("name", "wrong"), MUST_NOT_MATCH)
def test_a_different_team_is_never_matched(
    registry: tuple[TeamIndex, dict[str, str]], name: str, wrong: str
) -> None:
    index, teams = registry
    assert index.resolve(name).team_id != teams[wrong]


@pytest.mark.parametrize(
    "name",
    [
        "Aggies",  # Shared by several teams.
        "Tigers",
        "Bulldogs",
        "Saint",
        "State",
        "Unknown Community College",
        "Random Tech Academy",
        "Texas A&M Commerce Aggies",  # Two real teams' words mixed together.
        "",
    ],
)
def test_vague_or_unknown_names_stay_unresolved(
    registry: tuple[TeamIndex, dict[str, str]], name: str
) -> None:
    assert registry[0].resolve(name).team_id is None


def test_an_unresolved_name_offers_up_to_three_suggestions(
    registry: tuple[TeamIndex, dict[str, str]],
) -> None:
    index, teams = registry
    resolved = index.resolve("Pen Quakers Univ Pennsylvania X")
    assert resolved.team_id is None or resolved.team_id == teams["Pennsylvania Quakers"]
    unknown = index.resolve("Tigers of Somewhere")
    assert 1 <= len(unknown.suggestions) <= 3


def _team_tables(tmp_path: Path, extra: list[str] | None = None) -> None:
    sightings = teams_from_list("NCAA", json.loads(FIXTURE.read_text("utf-8")))
    names = [item.full_name for item in sightings] + (extra or [])
    teams = pl.DataFrame(
        {"league": ["NCAA"] * len(names), "team_id": [f"t{i}" for i in range(len(names))],
         "full_name": names}
    )  # fmt: skip
    aliases = pl.DataFrame(
        {"league": ["NCAA"], "alias_normalized": ["duke blue devils"], "team_id": ["t0"]}
    )
    for folder, frame, name in (
        ("teams", teams, "teams"),
        ("team_aliases", aliases, "team_aliases"),
    ):
        (tmp_path / "curated" / folder).mkdir(parents=True, exist_ok=True)
        frame.write_parquet(tmp_path / "curated" / folder / f"{name}.parquet")


def _with_every_hard_case(tmp_path: Path) -> None:
    hard = list(load_hard_cases()["NCAA"])
    known = {
        item.full_name for item in teams_from_list("NCAA", json.loads(FIXTURE.read_text("utf-8")))
    }
    _team_tables(tmp_path, [name for name in hard if name not in known])


def test_the_index_loads_aliases_hard_cases_and_owner_confirmations(tmp_path: Path) -> None:
    _with_every_hard_case(tmp_path)
    index = TeamIndex.load(tmp_path, "NCAA", {"mystery team": "t9"})
    assert index.resolve("Duke Blue Devils").team_id == "t0"
    assert index.resolve("Penn Quakers").team_id is not None  # From the hard cases.
    assert index.resolve("Mystery Team").team_id == "t9"


def test_a_hard_case_for_an_unknown_team_stops_the_load(tmp_path: Path) -> None:
    _team_tables(tmp_path)
    teams = pl.read_parquet(tmp_path / "curated/teams/teams.parquet")
    teams.head(1).write_parquet(tmp_path / "curated/teams/teams.parquet")
    with pytest.raises(ValueError, match="unknown team"):
        TeamIndex.load(tmp_path, "NCAA")


# Game matching, on a tiny registry.

TEAMS = {"duke blue devils": "d", "north carolina tar heels": "n", "kansas jayhawks": "k"}


def _game(
    game_id: str = "g1",
    home: str = "d",
    away: str = "n",
    tip: datetime = START,
    neutral: bool | None = False,
) -> CanonicalGame:
    return CanonicalGame(game_id, home, away, tip, neutral, date(2026, 11, 6))


def _match(
    games: list[CanonicalGame],
    home: str = "Duke Blue Devils",
    away: str = "North Carolina Tar Heels",
    start: datetime = START,
) -> object:
    return match_event(home, away, start, games, TeamIndex(TEAMS))


def test_a_unique_game_with_both_teams_matches() -> None:
    match = match_event(
        "Duke Blue Devils", "North Carolina Tar Heels", START, [_game()], TeamIndex(TEAMS)
    )
    assert (match.game_id, match.orientation, match.reason) == ("g1", NORMAL, None)
    assert match.matched


@pytest.mark.parametrize(
    ("hours", "matched"), [(0, True), (3, True), (-3, True), (3.01, False), (-3.01, False)]
)
def test_tip_times_must_be_within_three_hours(hours: float, matched: bool) -> None:
    start = START + timedelta(hours=hours)
    match = match_event(
        "Duke Blue Devils", "North Carolina Tar Heels", start, [_game()], TeamIndex(TEAMS)
    )
    assert match.matched is matched
    if not matched:
        assert match.reason == "no_game_found"


def test_two_candidate_games_are_not_guessed_between() -> None:
    games = [_game("g1"), _game("g2", tip=START + timedelta(hours=1))]
    match = match_event(
        "Duke Blue Devils", "North Carolina Tar Heels", START, games, TeamIndex(TEAMS)
    )
    assert not match.matched
    assert match.reason == "several_games_found"


def test_one_unresolved_team_means_no_match() -> None:
    match = match_event("Duke Blue Devils", "Mystery Team", START, [_game()], TeamIndex(TEAMS))
    assert not match.matched
    assert match.reason == "team_not_resolved"


def test_the_wrong_opponent_means_no_match() -> None:
    match = match_event("Duke Blue Devils", "Kansas Jayhawks", START, [_game()], TeamIndex(TEAMS))
    assert match.reason == "no_game_found"


@pytest.mark.parametrize("neutral", [True, None])
def test_a_neutral_game_listed_the_other_way_round_is_flagged_as_swapped(
    neutral: bool | None,
) -> None:
    match = match_event(
        "North Carolina Tar Heels",
        "Duke Blue Devils",
        START,
        [_game(neutral=neutral)],
        TeamIndex(TEAMS),
    )
    assert (match.game_id, match.orientation) == ("g1", SWAPPED)


def test_a_true_home_game_listed_the_other_way_round_goes_to_the_queue() -> None:
    match = match_event(
        "North Carolina Tar Heels",
        "Duke Blue Devils",
        START,
        [_game(neutral=False)],
        TeamIndex(TEAMS),
    )
    assert not match.matched
    assert match.reason == "swapped_at_a_true_home_game"


def test_the_same_team_twice_is_refused() -> None:
    match = match_event("Duke Blue Devils", "Duke Blue Devils", START, [_game()], TeamIndex(TEAMS))
    assert match.reason == "same_team_twice"


def test_an_owner_confirmed_alias_matches_from_then_on() -> None:
    aliases = {**TEAMS, **confirmed_aliases([{"alias_normalized": "mystery team", "team_id": "n"}])}
    match = match_event("Duke Blue Devils", "Mystery Team", START, [_game()], TeamIndex(aliases))
    assert match.game_id == "g1"
    assert match.away.method == "alias"
