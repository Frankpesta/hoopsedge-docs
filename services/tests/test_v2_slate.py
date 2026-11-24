"""v2 NCAAF slate (A3): brief §11.2 to §11.6, plus names, orientation, re-check and the CSV."""

import copy
import csv
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest

from hoopsedge.common.config_v2 import load_defaults
from hoopsedge.ingest.scores import RESULT_SCHEMA
from hoopsedge.l5.data import completed_results
from hoopsedge.l5.model import Result
from hoopsedge.sportybet.client import BlockedError
from hoopsedge.v2.legs import Leg, recheck
from hoopsedge.v2.lines import (
    AWAY_TOTAL_ID,
    HOME_TOTAL_ID,
    TOTAL_ID,
    EventLines,
    LineShapeError,
    booked_line,
    main_line,
    parse_event,
)
from hoopsedge.v2.names import (
    NORMAL,
    SWAPPED,
    Directory,
    EspnGame,
    Team,
    match_event,
    normalize,
    seed_aliases,
)
from hoopsedge.v2.slate import CSV_COLUMNS, Snapshot, build_slate, csv_rows, csv_text, tiers_for
from hoopsedge.v2.tickets import build_tickets
from hoopsedge.v2.verify import check_code

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = load_defaults()
NCAAF = CONFIG.sports["NCAAF"]
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 11, 28, 17, 0, tzinfo=UTC)


def registry() -> list[Team]:
    rows = json.loads((FIXTURES / "espn" / "ncaaf-registry-sample.json").read_text("utf-8"))
    return [
        Team(r["team_id"], r["display_name"], r["short_name"], r["location"], r["mascot"],
             r["level"])
        for r in rows
    ]  # fmt: skip


DIRECTORY = Directory(registry(), seed_aliases("NCAAF"))


# Names (brief §11.3, D89)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Delaware FightinÂ´ Blue Hens", "delaware fightin blue hens"),
        ("St. Thomas (MN) Tommies", "saint thomas mn tommies"),
        ("Morehead St Eagles", "morehead state eagles"),
        ("Hawai'i Rainbow Warriors", "hawaii rainbow warriors"),
        ("San José State Spartans", "san jose state spartans"),
        ("Louisiana-Monroe  Warhawks", "louisiana monroe warhawks"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("name", "team_id"),
    [
        ("Connecticut Huskies", "41"),
        ("Delaware FightinÂ´ Blue Hens", "48"),
        ("Middle Tennessee State Blue Raiders", "2393"),
        ("Miami Ohio RedHawks", "193"),
        ("FIU Panthers", "2229"),
    ],
)
def test_every_ncaaf_seed_alias_resolves(name: str, team_id: str) -> None:
    resolved = DIRECTORY.resolve(name)
    assert (resolved.team_id, resolved.method) == (team_id, "alias")


def test_every_seed_points_at_a_real_team() -> None:
    assert set(seed_aliases("NCAAF").values()) <= set(DIRECTORY.teams)


@pytest.mark.parametrize(
    "name",
    [
        "Florida Tech Panthers",  # Not FIU.
        "Georgia Bobcats",  # Not Georgia.
        "Minnesota State Mankato Mavericks",  # Not Minnesota.
        "St Thomas Aquinas Spartans",  # Not St. Thomas.
        "Valdosta State Blazers",  # A Division II name: queue, not a guess.
    ],
)
def test_known_false_positives_and_d2_names_go_to_the_queue(name: str) -> None:
    resolved = DIRECTORY.resolve(name)
    assert resolved.team_id is None
    assert resolved.reason == "unmatched"
    assert resolved.suggestions  # The queue gets suggestions, nothing more.


def test_exact_then_location_and_mascot() -> None:
    assert DIRECTORY.resolve("Southern Jaguars").team_id == "2582"
    assert DIRECTORY.resolve("South Alabama Jaguars").team_id == "6"
    found = DIRECTORY.resolve("Sam Houston State Bearkats")
    assert (found.team_id, found.method) == ("2534", "location_mascot")


def test_location_and_mascot_needs_exactly_one_candidate() -> None:
    twins = [
        Team("1", "North Alpha Owls", "N Alpha", "North Alpha", "Owls", "FBS"),
        Team("2", "Alpha Owls", "Alpha", "Alpha", "Owls", "FBS"),
    ]
    resolved = Directory(twins, {}).resolve("North Alpha State Owls")
    assert resolved.team_id is None and resolved.reason == "ambiguous"


def test_an_owner_alias_wins_over_the_exact_name() -> None:
    directory = Directory(registry(), {"Southern Jaguars": "6"})
    assert directory.resolve("Southern Jaguars").team_id == "6"


def test_an_alias_to_an_unknown_team_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown ESPN team"):
        Directory(registry(), {"Anyone": "999999"})


GAME = EspnGame("g1", "48", "41", KICKOFF, neutral=False)
NEUTRAL = EspnGame("g2", "48", "41", KICKOFF, neutral=True)


def test_event_matches_its_espn_game_within_three_hours() -> None:
    found = match_event(
        "Delaware Blue Hens", "UConn Huskies", KICKOFF + timedelta(hours=2), [GAME], DIRECTORY
    )
    assert (found.game, found.orientation) == (GAME, NORMAL)
    late = match_event(
        "Delaware Blue Hens", "UConn Huskies", KICKOFF + timedelta(hours=4), [GAME], DIRECTORY
    )
    assert late.game is None and late.reason == "no_espn_game_within_3_hours"


def test_a_swapped_listing_matches_only_at_a_neutral_site() -> None:
    swapped = match_event("UConn Huskies", "Delaware Blue Hens", KICKOFF, [NEUTRAL], DIRECTORY)
    assert swapped.orientation == SWAPPED
    refused = match_event("UConn Huskies", "Delaware Blue Hens", KICKOFF, [GAME], DIRECTORY)
    assert refused.game is None and refused.reason == "swapped_at_a_true_home_game"


# Lines (brief §11.2)


def ladder_event(
    lines: list[float],
    *,
    main: float,
    event_id: str = "sr:match:1",
    home: str = "Delaware Blue Hens",
    away: str = "UConn Huskies",
    start: datetime = KICKOFF,
    team_totals: tuple[float | None, float | None] = (30.5, 24.5),
    match_status: str = "Not start",
) -> dict[str, Any]:
    """An event detail shaped like SportyBet's: odds widen away from the main line."""

    def market(market_id: str, line: float, gap: float) -> dict[str, Any]:
        over = round(1.87 - 0.06 * gap, 2)
        under = round(1.87 + 0.06 * gap, 2)
        return {
            "id": market_id,
            "specifier": f"total={line}",
            "status": 0,
            "outcomes": [
                {"id": "12", "odds": f"{over:.2f}", "isActive": 1, "desc": f"Over {line}"},
                {"id": "13", "odds": f"{under:.2f}", "isActive": 1, "desc": f"Under {line}"},
            ],
        }

    markets = [market(TOTAL_ID, line, line - main) for line in lines]
    for market_id, line in zip((HOME_TOTAL_ID, AWAY_TOTAL_ID), team_totals, strict=True):
        if line is not None:
            markets.append(market(market_id, line, 0.0))
    return {
        "eventId": event_id,
        "homeTeamName": home,
        "awayTeamName": away,
        "estimateStartTime": int(start.timestamp() * 1000),
        "status": 0,
        "matchStatus": match_status,
        "markets": markets,
    }


@pytest.mark.parametrize("depth", [5, 15])
def test_main_and_safest_lines(depth: int) -> None:
    centre = 50.5
    lines = [centre + step for step in range(-(depth // 2), depth // 2 + 1)]
    event = parse_event(ladder_event(lines, main=centre))
    assert len(event.totals) == depth
    main = main_line(event.totals)
    assert main is not None and main.line == centre
    over = booked_line(event.totals, "safest", "Over", 60.0, 0.0)
    under = booked_line(event.totals, "safest", "Under", 40.0, 0.0)
    assert over is not None and over.line == min(lines)
    assert under is not None and under.line == max(lines)
    assert over.outcome("Over")[0] == "12"
    assert under.outcome("Under")[0] == "13"
    with pytest.raises(ValueError, match="Unknown line mode 'main'"):
        booked_line(event.totals, "main", "Over", 60.0, 0.0)  # D101: never booked.


def test_the_real_ncaaf_event_parses() -> None:
    document = json.loads((FIXTURES / "sportybet" / "ncaaf-event-detail.json").read_text("utf-8"))
    event = parse_event(document["data"])
    assert [line.line for line in event.totals] == [45.5, 46.5, 47.5]
    main = main_line(event.totals)
    assert main is not None and main.line == 46.5  # 1.81 / 1.90 is the closest pair.
    assert [line.line for line in event.home_totals] == [26.5]
    assert [line.line for line in event.away_totals] == [19.5]
    assert event.not_started(datetime(2026, 11, 23, tzinfo=UTC))


def test_main_line_ties_go_to_the_middle_then_the_lower_line() -> None:
    event = parse_event(ladder_event([48.5, 49.5, 50.5, 51.5], main=50.0))
    main = main_line(event.totals)  # 49.5 and 50.5 are equally even; both are mid; take lower.
    assert main is not None and main.line == 49.5


def test_cushion_mode_books_only_with_enough_room() -> None:
    event = parse_event(ladder_event([48.5, 49.5, 50.5], main=49.5))
    assert booked_line(event.totals, "safest_with_cushion", "Over", 50.0, 1.0) is not None
    assert booked_line(event.totals, "safest_with_cushion", "Over", 49.0, 1.0) is None


def test_suspended_and_inactive_lines_are_not_offered() -> None:
    document = ladder_event([48.5, 49.5, 50.5], main=49.5)
    document["markets"][0]["status"] = 2
    document["markets"][2]["outcomes"][0]["isActive"] = 0
    assert [line.line for line in parse_event(document).totals] == [49.5]


@pytest.mark.parametrize(
    "breakage",
    [
        lambda m: m["outcomes"][0].update(desc="Over 99.5"),
        lambda m: m["outcomes"][1].update(desc="Over 48.5"),
        lambda m: m.update(specifier="hcp=-3.5"),
        lambda m: m["outcomes"].pop(),
    ],
    ids=["desc-number", "two-overs", "specifier", "one-outcome"],
)
def test_a_line_of_an_unknown_shape_stops_the_event(breakage: Any) -> None:
    document = ladder_event([48.5], main=48.5)
    breakage(document["markets"][0])
    with pytest.raises(LineShapeError):
        parse_event(document)


# Legs, re-check and the started-game guard (brief §11.4)


def lean_leg(event: EventLines, projection: float = 58.0) -> Leg:
    main = main_line(event.totals)
    booked = booked_line(event.totals, "safest", "Over", projection, 0.0)
    assert main is not None and booked is not None
    return Leg(
        kind="TOTALS_LEAN", event_id=event.event_id, game_id="g1", home="Delaware", away="UConn",
        kickoff=event.start, mode="safest", team=None, direction="Over", projection=projection,
        market_id=booked.market_id, specifier=booked.specifier, outcome_id=booked.over_id,
        odds=booked.over_odds, booked_line=booked.line, main_line=main.line,
        main_odds=main.over_odds, edge_main=projection - main.line,
        edge_booked=projection - booked.line, flags=(),
    )  # fmt: skip


CUTOFF = timedelta(minutes=20)


def test_a_game_that_started_between_predict_and_book_is_dropped() -> None:
    event = parse_event(ladder_event([50.5, 51.5, 52.5], main=51.5))
    leg = lean_leg(event)
    live = parse_event(ladder_event([50.5, 51.5, 52.5], main=51.5, match_status="1st quarter"))
    assert recheck(leg, live, NCAAF, NOW, CUTOFF) == (None, "started")
    begun = parse_event(ladder_event([50.5], main=50.5, start=NOW - timedelta(minutes=1)))
    assert recheck(leg, begun, NCAAF, NOW, CUTOFF) == (None, "started")


def test_a_game_inside_the_cutoff_is_dropped() -> None:
    event = parse_event(ladder_event([50.5], main=50.5, start=NOW + timedelta(minutes=19)))
    assert recheck(lean_leg(event), event, NCAAF, NOW, CUTOFF) == (None, "inside_cutoff")


def test_recheck_reselects_lines_from_the_fresh_ladder() -> None:
    leg = lean_leg(parse_event(ladder_event([50.5, 51.5, 52.5], main=51.5)))
    moved = parse_event(ladder_event([51.5, 52.5, 53.5], main=52.5))
    checked, reason = recheck(leg, moved, NCAAF, NOW, CUTOFF)
    assert reason is None and checked is not None
    assert (checked.main_line, checked.booked_line) == (52.5, 51.5)
    assert checked.edge_main == pytest.approx(58.0 - 52.5)


def test_recheck_drops_a_lean_the_moved_line_no_longer_supports() -> None:
    leg = lean_leg(parse_event(ladder_event([50.5, 51.5, 52.5], main=51.5)), projection=56.0)
    moved = parse_event(ladder_event([52.5, 53.5, 54.5], main=53.5))  # Edge 2.5 < 4.0.
    assert recheck(leg, moved, NCAAF, NOW, CUTOFF) == (None, "lean_gone")


# Tickets (brief §11.5)


def test_47_legs_split_20_20_7_by_kickoff() -> None:
    base = parse_event(ladder_event([50.5], main=50.5))
    legs = [
        lean_leg(
            parse_event(
                ladder_event([50.5], main=50.5, event_id=f"sr:match:{i}",
                             start=KICKOFF + timedelta(minutes=10 * ((i * 7) % 47)))
            )
        )
        for i in range(47)
    ]  # fmt: skip
    assert base.totals
    tickets, dropped = build_tickets("TOTALS_LEAN", legs, CONFIG.tickets, NOW)
    assert [len(ticket.legs) for ticket in tickets] == [20, 20, 7]
    assert dropped == []
    order = [leg.kickoff for ticket in tickets for leg in ticket.legs]
    assert order == sorted(order)
    assert tickets[0].earliest_kickoff == KICKOFF


def test_a_lone_last_leg_is_not_booked_and_nothing_pads() -> None:
    legs = [
        lean_leg(parse_event(ladder_event([50.5], main=50.5, event_id=f"sr:match:{i}",
                                          start=KICKOFF + timedelta(minutes=i))))
        for i in range(21)
    ]  # fmt: skip
    tickets, dropped = build_tickets("TOTALS_LEAN", legs, CONFIG.tickets, NOW)
    assert [len(ticket.legs) for ticket in tickets] == [20]
    assert [item.reason for item in dropped] == ["ticket_below_min_legs"]


def test_only_the_top_100_by_edge_are_kept() -> None:
    legs = [
        lean_leg(parse_event(ladder_event([50.5], main=50.5, event_id=f"sr:match:{i}")),
                 projection=55.0 + i / 10)
        for i in range(105)
    ]  # fmt: skip
    tickets, dropped = build_tickets("TOTALS_LEAN", legs, CONFIG.tickets, NOW)
    assert sum(len(ticket.legs) for ticket in tickets) == 100
    assert {item.leg.event_id for item in dropped} == {f"sr:match:{i}" for i in range(5)}


def test_ticket_odds_and_bookmaker_implied_probability() -> None:
    legs = [
        lean_leg(parse_event(ladder_event([50.5], main=50.5, event_id=f"sr:match:{i}")))
        for i in range(2)
    ]
    [ticket] = build_tickets("TOTALS_LEAN", legs, CONFIG.tickets, NOW)[0]
    assert ticket.total_odds == round(1.87 * 1.87, 2)
    assert ticket.bookmaker_implied == pytest.approx(1 / 1.87**2)
    assert ticket.selections()[0] == {
        "eventId": "sr:match:0", "marketId": "225", "specifier": "total=50.5", "outcomeId": "12",
    }  # fmt: skip


# Verification (brief §11.6)


def lookup(selections: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "bizCode": 10000,
        "data": {
            "outcomes": [
                {
                    "eventId": item["eventId"],
                    "markets": [
                        {
                            "id": item["marketId"],
                            "specifier": item["specifier"],
                            "outcomes": [{"id": item["outcomeId"], "odds": "1.87"}],
                        }
                    ],
                }
                for item in selections
            ]
        },
    }


SELECTIONS = [
    {"eventId": f"sr:match:{i}", "marketId": "225", "specifier": "total=50.5", "outcomeId": "12"}
    for i in range(3)
]


def test_a_code_matching_every_selection_is_verified() -> None:
    assert check_code(SELECTIONS, lookup(SELECTIONS)).verified


def test_a_code_with_the_wrong_leg_count_fails_loudly() -> None:
    result = check_code(SELECTIONS, lookup(SELECTIONS[:2]))
    assert not result.verified
    assert result.reason == "2 legs in the code, 3 intended"


def test_a_code_with_a_different_selection_fails() -> None:
    changed = copy.deepcopy(SELECTIONS)
    changed[1]["outcomeId"] = "13"
    result = check_code(SELECTIONS, lookup(changed))
    assert not result.verified and result.missing and result.extra


def test_an_unreadable_lookup_fails() -> None:
    assert not check_code(SELECTIONS, {"data": {}}).verified


# A whole slate, end to end without HTTP


def results_frame(neutral: bool = False) -> pl.DataFrame:
    """Delaware (48) scores 35 and allows 20; UConn (41) scores 30 and allows 21; then they meet.
    Projections: Delaware (35 + 21) / 2 = 28, UConn (30 + 20) / 2 = 25, total 53."""
    rows = []
    for week in range(1, 6):
        day = KICKOFF - timedelta(weeks=week)
        rows.append(_result(f"d{week}", "48", "2226", day, 35, 20))
        rows.append(_result(f"u{week}", "41", "2229", day, 30, 21))
    rows.append(_result("slate", "48", "41", KICKOFF, None, None, neutral))
    return pl.DataFrame(rows, schema=RESULT_SCHEMA)


def _result(
    game_id: str,
    home: str,
    away: str,
    kickoff: datetime,
    hp: int | None,
    ap: int | None,
    neutral: bool = False,
) -> dict[str, Any]:
    done = hp is not None
    return {
        "sport": "NCAAF", "season": 2026, "season_type": 2, "week": 1, "game_id": game_id,
        "kickoff_utc": kickoff, "slate_date_et": kickoff.date(), "home_id": home,
        "away_id": away, "neutral": neutral, "status": "STATUS_FINAL" if done else "x",
        "state": "post" if done else "pre", "completed": done, "home_pts": hp, "away_pts": ap,
    }  # fmt: skip


LEVELS = {team.team_id: team.level for team in registry()}


def slate_build(events: list[dict[str, Any]], neutral: bool = False) -> Any:
    frame = results_frame(neutral)
    details = {event["eventId"]: event for event in events}

    def detail(event_id: str) -> Snapshot:
        if event_id == "sr:match:blocked":
            raise BlockedError("challenge page")
        return Snapshot(parse_event(details[event_id]), NOW)

    return build_slate(
        "NCAAF", date(2026, 11, 28), CONFIG, events, detail, frame, completed_results(frame),
        LEVELS, DIRECTORY,
    )  # fmt: skip


def test_slate_build_predicts_and_builds_legs() -> None:
    build = slate_build([ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5))])
    [row] = build.rows
    p = row.prediction
    assert (p.home_proj, p.away_proj, p.model_total) == (28.0, 25.0, 53.0)
    assert (p.main_line, p.lean) == (48.5, "OVER")  # Edge +4.5 against the main line.
    lean = row.legs["TOTALS_LEAN"]
    assert (lean.booked_line, lean.main_line, lean.mode) == (47.5, 48.5, "safest")
    assert (lean.edge_main, lean.edge_booked) == (4.5, 5.5)
    team = row.legs["TEAM_TOTALS"]
    assert (team.team, team.direction, team.market_id, team.booked_line) == (
        "home", "Over", HOME_TOTAL_ID, 23.5,
    )  # fmt: skip
    assert row.other_variant.model_total == 53.0  # Same-level variant: all FBS opponents here.
    assert build.unmatched == [] and build.failures == []


def test_team_totals_follow_a_swapped_listing() -> None:
    """Audit item 13: SportyBet lists UConn at home for a neutral game ESPN gives Delaware."""
    event = ladder_event(
        [47.5, 48.5, 49.5], main=48.5, home="UConn Huskies", away="Delaware Blue Hens",
        team_totals=(26.5, 23.5),
    )  # fmt: skip
    [row] = slate_build([event], neutral=True).rows
    assert row.orientation == SWAPPED
    team = row.legs["TEAM_TOTALS"]
    # Delaware (ESPN home, projected 28) is SportyBet's away team: market 228 at 23.5.
    assert (team.team, team.direction, team.market_id, team.booked_line) == (
        "home", "Over", AWAY_TOTAL_ID, 23.5,
    )  # fmt: skip


def test_unmatched_and_other_day_events_never_get_rows() -> None:
    events = [
        ladder_event([48.5], main=48.5, event_id="sr:match:d2", home="Valdosta State Blazers"),
        ladder_event([48.5], main=48.5, event_id="sr:match:later",
                     start=KICKOFF + timedelta(days=3)),
    ]  # fmt: skip
    build = slate_build(events)
    assert build.rows == []
    [queued] = build.unmatched
    assert queued["event_id"] == "sr:match:d2"
    assert queued["home"]["reason"] == "unmatched"


def test_a_blocked_detail_stops_the_whole_run() -> None:
    with pytest.raises(BlockedError):
        slate_build([ladder_event([48.5], main=48.5, event_id="sr:match:blocked")])


def test_an_event_of_unknown_shape_is_skipped_and_recorded() -> None:
    event = ladder_event([48.5], main=48.5)
    event["markets"][0]["outcomes"][0]["desc"] = "Over 99.5"
    build = slate_build([event])
    assert build.rows == []
    assert build.failures[0]["reason"].startswith("LineShapeError")


def test_csv_has_the_owner_columns_and_values() -> None:
    build = slate_build([ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5))])
    text = csv_text(csv_rows(build, ZoneInfo("Africa/Lagos")))
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames == CSV_COLUMNS
    [row] = list(reader)
    assert row["Away"] == "UConn" and row["Home"] == "Delaware"
    assert row["Away L5 PF/PA"] == "30.0/21.0" and row["Home L5 PF/PA"] == "35.0/20.0"
    assert (row["Model Total"], row["Vegas Total"], row["Lean"]) == ("53.0", "48.5", "OVER")
    assert row["Kickoff (WAT)"] == "2026-11-28 18:00"  # 17:00 UTC.
    assert (row["Booked Line"], row["Edge vs Main"], row["Edge vs Booked"]) == (
        "47.5", "4.5", "5.5",
    )  # fmt: skip
    assert (row["Team Pick"], row["Team Line"]) == ("Delaware Over", "23.5")
    assert row["Away Atk/Def"] == "Medium/Medium" and row["Home Atk/Def"] == "High/High"


# Basketball (B2)


def ncaab_registry() -> list[Team]:
    rows = json.loads((FIXTURES / "espn" / "ncaab-registry-sample.json").read_text("utf-8"))
    return [
        Team(r["team_id"], r["display_name"], r["short_name"], r["location"], r["mascot"],
             r["level"])
        for r in rows
    ]  # fmt: skip


NCAAB_DIRECTORY = Directory(ncaab_registry(), seed_aliases("NCAAB"))


@pytest.mark.parametrize(("name", "team_id"), sorted(seed_aliases("NCAAB").items()))
def test_every_ncaab_seed_alias_resolves(name: str, team_id: str) -> None:
    resolved = NCAAB_DIRECTORY.resolve(name)
    assert (resolved.team_id, resolved.method) == (team_id, "alias")


def test_a_high_fuzzy_score_alone_never_matches() -> None:
    """Without its alias, 'Alabama AM Bulldogs' scores 97 against Alabama A&M: queue only."""
    bare = Directory(ncaab_registry(), {})
    resolved = bare.resolve("Alabama AM Bulldogs")
    assert resolved.team_id is None
    assert resolved.suggestions[0][0] == "2010" and resolved.suggestions[0][1] > 95


def test_basketball_tiers_are_provisional_percentiles_of_the_main_level() -> None:
    finals = []
    for index in range(1, 11):  # D1 teams 1..10 score 60 + 3i and allow 80 - 2i.
        for week in range(1, 6):
            kickoff = KICKOFF - timedelta(days=week)
            finals.append(Result(2027, f"{index}-{week}", kickoff, str(index), f"o{index}",
                                 60 + 3 * index, 80 - 2 * index))  # fmt: skip
    levels = {str(index): "D1" for index in range(1, 11)}
    levels |= {f"o{index}": "OTHER" for index in range(1, 11)}
    config = CONFIG.sports["NCAAB"]
    tiers = tiers_for("NCAAB", date(2026, 11, 28), config, finals, 2027, levels)
    assert tiers.provisional
    pf = sorted(60 + 3 * i for i in range(1, 11))
    assert tiers.atk == pytest.approx((float(np.percentile(pf, 100 / 3)),
                                       float(np.percentile(pf, 200 / 3))))  # fmt: skip
    # Only games before the slate's Eastern day count: on the 25th each team has just two.
    with pytest.raises(ValueError, match="no tiers yet"):
        tiers_for("NCAAB", date(2026, 11, 25), config, finals, 2027, levels)
