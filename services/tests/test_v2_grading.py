"""v2 grading and calibration (brief §9, D90): results, both variants, line modes, suggestions."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hoopsedge.pipeline.schedule import due_jobs
from hoopsedge.v2.booking import Booker, book_ticket, record_run
from hoopsedge.v2.config_store import (
    SCHEDULE_NAMES,
    ConfigDocument,
    default_document,
    document_from_stored,
)
from hoopsedge.v2.grading import (
    FINAL,
    MIN_GRADED,
    GradedLeg,
    Outcome,
    apply_suggestion,
    bucket_name,
    calibration,
    grade,
    grade_slate,
    last_rows_before_kickoff,
    outcomes_from,
    row_legs,
    slates_to_grade,
    stats,
    suggestions,
    ticket_result,
    write_report,
)
from hoopsedge.v2.lines import HOME_TOTAL_ID
from hoopsedge.v2.slate import _jsonable, _row_document, ticket_document
from hoopsedge.v2.tickets import build_tickets
from hoopsedge.v2.views import find_ticket, grades_path
from tests.test_v2_slate import CONFIG, KICKOFF, NOW, ladder_event, slate_build

SLATE = date(2026, 11, 28)
ZONE = ZoneInfo("Africa/Lagos")
DOCUMENT = default_document().model_dump(by_alias=True, mode="json")


def run_document(run_id: str, started: datetime = NOW, rows: Any = None) -> dict[str, Any]:
    """Delaware (28 projected) hosts UConn (25): total 53 against a 48.5 main line, an Over."""
    build = slate_build([ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5))])
    document = {
        "run_id": run_id, "sport": "NCAAF", "slate": SLATE.isoformat(), "trigger": "scheduled",
        "status": "succeeded", "started_at": started.isoformat(),
        "config": CONFIG.model_dump(by_alias=True, mode="json"),
        "rows": rows if rows is not None else [_row_document(row) for row in build.rows],
        "tickets": [],
    }  # fmt: skip
    return json.loads(json.dumps(document, default=_jsonable))  # As a run writes it.


def write(root: Path, document: dict[str, Any]) -> Path:
    folder = root / "v2" / "runs" / "NCAAF" / SLATE.isoformat() / document["run_id"]
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps(document, default=_jsonable), "utf-8")
    return folder


# Settling one leg.


@pytest.mark.parametrize(
    ("line", "direction", "home", "away", "expected"),
    [
        (48.5, "Over", 30, 20, "won"),
        (48.5, "Over", 28, 20, "lost"),
        (48.5, "Under", 28, 20, "won"),
        (48.0, "Over", 28, 20, "push"),
    ],
)
def test_a_total_settles_on_the_final_score(
    line: float, direction: str, home: int, away: int, expected: str
) -> None:
    result, actual = grade("totals", line, None, direction, Outcome(FINAL, home, away))
    assert (result, actual) == (expected, home + away)


def test_a_team_total_settles_on_that_team_only() -> None:
    assert grade("team_total", 23.5, "away", "Over", Outcome(FINAL, 40, 20)) == ("lost", 20)
    assert grade("team_total", 23.5, "home", "Over", Outcome(FINAL, 40, 20)) == ("won", 40)


def test_unfinished_and_cancelled_games() -> None:
    rows = [
        {"game_id": "a", "completed": True, "status": "STATUS_FINAL", "home_pts": 3, "away_pts": 1},
        {"game_id": "b", "completed": False, "status": "STATUS_CANCELED",
         "home_pts": None, "away_pts": None},
        {"game_id": "c", "completed": False, "status": "STATUS_SCHEDULED",
         "home_pts": None, "away_pts": None},
    ]  # fmt: skip
    outcomes = outcomes_from(rows)
    assert outcomes["a"] == Outcome(FINAL, 3, 1)
    assert outcomes["b"].status == "void"
    assert "c" not in outcomes
    assert grade("totals", 40.5, None, "Over", Outcome("pending")) == ("pending", None)


# Graded legs per game.


def test_each_game_is_graded_for_both_variants_at_main_and_safest_lines() -> None:
    document = run_document("01A")
    legs = row_legs(document, document["rows"][0], Outcome(FINAL, 30, 20))
    seen = {(leg.same_level, leg.market, leg.mode) for leg in legs}
    assert seen == {
        (same, market, mode)
        for same in (False, True)
        for market in ("totals", "team_total")
        for mode in ("main", "safest")
    }
    active = {(leg.market, leg.mode): leg for leg in legs if leg.active}
    assert all(not leg.same_level for leg in active.values())  # The default is off.
    main, safest = active[("totals", "main")], active[("totals", "safest")]
    assert (main.direction, main.line, main.odds, main.edge) == ("Over", 48.5, 1.87, 4.5)
    assert (safest.line, safest.result, safest.actual) == (47.5, "won", 50)
    team = active[("team_total", "main")]
    # Home's edge is 28 - 23.5 = 4.5 against away's 25 - 24.5 = 0.5.
    assert (team.team, team.direction, team.line, team.result, team.actual) == (
        "home", "Over", 23.5, "won", 30,
    )  # fmt: skip
    assert active[("team_total", "safest")].line == 23.5  # A one-line ladder: its only line.


def test_team_totals_book_and_grade_the_lowest_odds_line() -> None:
    """D101: SportyBet lists about five team-total lines. The Over books the lowest line (the
    lowest odds), and grading grades the same line."""
    event = ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5))
    for line, gap in ((21.5, -2), (22.5, -1), (24.5, 1), (25.5, 2)):
        # As on SportyBet: the further below the main line, the cheaper the Over.
        prices = {"Over": round(1.87 + 0.12 * gap, 2), "Under": round(1.87 - 0.12 * gap, 2)}
        outcomes = [
            {"id": key, "odds": f"{prices[word]:.2f}", "isActive": 1, "desc": f"{word} {line}"}
            for key, word in (("12", "Over"), ("13", "Under"))
        ]
        event["markets"].append(
            {"id": HOME_TOTAL_ID, "specifier": f"total={line}", "status": 0, "outcomes": outcomes}
        )
    [row] = slate_build([event]).rows
    leg = row.legs["TEAM_TOTALS"]
    assert (leg.team, leg.direction, leg.mode) == ("home", "Over", "safest")
    assert (leg.booked_line, leg.odds) == (21.5, 1.63)  # Not the main 23.5 at 1.87.
    assert (leg.main_line, leg.main_odds) == (23.5, 1.87)  # Kept as evidence only.
    stored = _row_document(row)["team_lines"]["home"]
    assert stored["line"] == 23.5
    assert stored["lowest"] == {"line": 21.5, "over_odds": 1.63, "under_odds": 2.11}
    assert stored["highest"]["line"] == 25.5
    document = run_document("01A", rows=[_row_document(row)])
    legs = row_legs(document, document["rows"][0], Outcome(FINAL, 22, 20))
    safest = next(g for g in legs if g.active and (g.market, g.mode) == ("team_total", "safest"))
    assert (safest.line, safest.odds, safest.result) == (21.5, 1.63, "won")


def test_a_game_below_threshold_is_still_graded_so_any_threshold_can_be_judged() -> None:
    build = slate_build([ladder_event([50.5, 51.5, 52.5], main=51.5, team_totals=(23.5, 24.5))])
    [row] = build.rows
    assert row.prediction.lean == "PASS"  # Edge +1.5, under the 4.0 over threshold.
    document = run_document("01A", rows=[_row_document(row)])
    legs = row_legs(document, document["rows"][0], Outcome(FINAL, 30, 20))
    totals = [leg for leg in legs if leg.market == "totals" and leg.active]
    assert {leg.edge for leg in totals} == {1.5}


def test_a_game_takes_the_last_lines_seen_before_its_kickoff() -> None:
    early = run_document("01A", NOW)
    later = run_document("01B", NOW + timedelta(hours=2))
    late = run_document("01C", KICKOFF + timedelta(minutes=5))  # Live lines: never graded.
    later["rows"][0]["legs"]["TOTALS_ALL_OVER"]["main_line"] = 49.5
    [(run, row)] = last_rows_before_kickoff([early, later, late])
    assert run["run_id"] == "01B"
    assert row["legs"]["TOTALS_ALL_OVER"]["main_line"] == 49.5


# Slates and tickets.


def test_grading_a_slate_grades_its_tickets_and_writes_a_file(tmp_path: Path) -> None:
    document = run_document("01A")
    # Two SportyBet events on the one ESPN game, so one final settles a two-leg ticket.
    two = slate_build(
        [
            ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5)),
            ladder_event([47.5, 48.5, 49.5], main=48.5, event_id="sr:match:2",
                         start=KICKOFF + timedelta(minutes=30), team_totals=(23.5, 24.5)),
        ]
    )  # fmt: skip
    [lean] = build_tickets("TOTALS_LEAN", [r.legs["TOTALS_LEAN"] for r in two.rows],
                           CONFIG.tickets, NOW)[0]  # fmt: skip
    booked = book_ticket(Booker(None, False, lambda *_: None), lean, "T1",
                         lambda given: (list(given), []), 2)  # fmt: skip
    document["tickets"] = [ticket_document(lean, "T1", booked, ZONE)]
    folder = write(tmp_path, document)
    record_run(folder.parent, "01A", "scheduled", document["tickets"])

    pending = grade_slate(tmp_path, "NCAAF", SLATE, {}, NOW)
    assert pending["tickets"]["T1"]["result"] == "pending"
    assert pending["pending"] > 0

    graded = grade_slate(tmp_path, "NCAAF", SLATE, {"slate": Outcome(FINAL, 30, 20)}, NOW)
    assert graded["tickets"]["T1"]["result"] == "won"  # Both legs on one ESPN game, total 50.
    assert json.loads(grades_path(tmp_path, "NCAAF", SLATE).read_text("utf-8")) == graded
    shown = find_ticket(tmp_path, "T1", NOW)
    assert shown is not None
    assert shown["result"] == "won"
    assert [leg["result"] for leg in shown["legs"]] == ["won", "won"]
    assert shown["legs"][0]["actual"] == 50


def test_a_slate_from_before_the_index_grades_its_newest_run_tickets(tmp_path: Path) -> None:
    document = run_document("01A")
    document["tickets"] = [{"ticket_id": "T0", "leg_details": [
        {"event_id": "sr:match:1", "game_id": "slate", "team": None, "direction": "Over",
         "booked_line": 47.5}]}]  # fmt: skip
    write(tmp_path, document)
    graded = grade_slate(tmp_path, "NCAAF", SLATE, {"slate": Outcome(FINAL, 30, 20)}, NOW)
    assert graded["tickets"]["T0"]["result"] == "won"


def test_only_finished_slates_with_something_pending_are_regraded(tmp_path: Path) -> None:
    write(tmp_path, run_document("01A"))
    assert slates_to_grade(tmp_path, "NCAAF", SLATE) == []  # Not over yet.
    assert slates_to_grade(tmp_path, "NCAAF", SLATE + timedelta(days=1)) == [SLATE]
    grade_slate(tmp_path, "NCAAF", SLATE, {"slate": Outcome(FINAL, 30, 20)}, NOW)
    assert slates_to_grade(tmp_path, "NCAAF", SLATE + timedelta(days=1)) == []


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (["won", "won"], "won"),
        (["won", "lost", "pending"], "lost"),
        (["won", "pending"], "pending"),
        (["won", "push", "void"], "won"),
        (["void", "push"], "void"),
        ([], None),
    ],
)
def test_ticket_results(results: list[str], expected: str | None) -> None:
    assert ticket_result(results) == expected


# Calibration (D90).


def leg(edge: float, result: str, odds: float = 1.9, slate: int = 1, **changes: Any) -> GradedLeg:
    values: dict[str, Any] = {
        "sport": "NCAAB", "slate": f"2026-11-{slate:02d}", "run_id": "r", "game_id": "g",
        "event_id": "e", "market": "totals", "same_level": False, "active": True,
        "mode": "safest", "team": None, "direction": "Over" if edge > 0 else "Under",
        "line": 140.5, "odds": odds, "edge": edge, "cross_level": False, "result": result,
        "actual": 150,
    }  # fmt: skip
    return GradedLeg(**{**values, **changes})


def test_hit_rate_is_judged_against_the_implied_rate_and_flat_stake_roi() -> None:
    numbers = stats([leg(5, "won", 2.0), leg(5, "lost", 2.0), leg(5, "push", 2.0),
                     leg(5, "pending", 2.0), leg(5, "void", 2.0)])  # fmt: skip
    assert (numbers.legs, numbers.won, numbers.lost, numbers.push) == (3, 1, 1, 1)
    assert numbers.hit_rate == 0.5
    assert numbers.implied == 0.5
    assert numbers.above_implied == 0.0
    assert numbers.roi == pytest.approx(0.0)  # +1, -1 and a push, over 3 units staked.


@pytest.mark.parametrize(
    ("edge", "name"), [(0.0, "0–2"), (1.99, "0–2"), (-2.0, "2–4"), (9.9, "6–10"), (-12, "10+")]
)
def test_edge_buckets(edge: float, name: str) -> None:
    assert bucket_name(edge) == name


def season(per_slate: int = 20, slates: int = 10) -> list[GradedLeg]:
    """Overs at edges 0.5 to 10: every leg at 4 or more wins, everything under it loses."""
    return [
        leg(0.5 * (i + 1), "won" if 0.5 * (i + 1) >= 4 else "lost", slate=day)
        for day in range(1, slates + 1)
        for i in range(per_slate)
    ]


def test_a_suggestion_needs_100_graded_legs_and_maximises_roi() -> None:
    config = DOCUMENT["sports"]["NCAAB"]
    found = {(s["market"], s["mode"]): s for s in suggestions(season(), config, 2)}
    # D101: only the lowest-odds line is booked, so it is the only line mode suggested.
    assert set(found) == {("totals", "safest"), ("team_total", "safest")}
    main = found[("totals", "safest")]
    assert main["suggested"]["thresholds"]["over_threshold"] == 4.0  # Ties go to more legs.
    assert main["suggested"]["numbers"]["legs"] == 130 >= MIN_GRADED
    assert main["suggested"]["numbers"]["roi"] == pytest.approx(0.9)
    assert main["current"]["thresholds"]["over_threshold"] == 5.0
    assert found[("team_total", "safest")]["suggested"] is None  # No legs in that market.
    main_legs = [replace_leg(item, mode="main") for item in season()]
    assert all(s["graded"] == 0 for s in suggestions(main_legs, config, 2))  # Evidence only.
    few = suggestions(season(slates=4), config, 2)
    assert few[0]["suggested"] is None  # 80 legs in all: not enough to suggest anything.


def test_a_suggestion_keeps_min_legs_per_slate() -> None:
    config = DOCUMENT["sports"]["NCAAB"]
    [main, *_] = suggestions(season(slates=12), config, 14)
    # 4.0 keeps 13 legs a slate, under 14; only lower thresholds, with losers, qualify.
    assert main["suggested"]["thresholds"]["over_threshold"] == 3.5


def test_suggestions_use_the_variant_in_force_only() -> None:
    other = [replace_leg(item, same_level=True) for item in season()]
    [main, *_] = suggestions(other, DOCUMENT["sports"]["NCAAB"], 2)
    assert main["graded"] == 0 and main["suggested"] is None


def replace_leg(item: GradedLeg, **changes: Any) -> GradedLeg:
    return GradedLeg(**{**item.__dict__, **changes})


def test_applying_a_suggestion_changes_thresholds_and_line_mode_only() -> None:
    changed = apply_suggestion(DOCUMENT, "NCAAB", "totals", "safest_with_cushion",
                               {"over_threshold": 4.0, "under_threshold": 6.5})  # fmt: skip
    sport = changed["sports"]["NCAAB"]
    assert (sport["over_threshold"], sport["under_threshold"]) == (4.0, 6.5)
    assert sport["line_mode"]["totals"] == "safest_with_cushion"
    assert DOCUMENT["sports"]["NCAAB"]["line_mode"]["totals"] == "safest"  # Not touched.
    sport_before = {k: v for k, v in DOCUMENT["sports"]["NCAAB"].items()
                    if k not in ("over_threshold", "under_threshold", "line_mode")}  # fmt: skip
    assert {k: sport[k] for k in sport_before} == sport_before


def write_grades(root: Path, legs: list[GradedLeg]) -> None:
    by_slate: dict[str, list[GradedLeg]] = {}
    for item in legs:
        by_slate.setdefault(item.slate, []).append(item)
    for slate, items in by_slate.items():
        path = grades_path(root, items[0].sport, date.fromisoformat(slate))
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"pending": 0, "legs": [item.__dict__ for item in items],
                    "tickets": {"T": {"result": "lost", "legs": []}}}  # fmt: skip
        path.write_text(json.dumps(document), "utf-8")


def test_calibration_and_the_weekly_report(tmp_path: Path) -> None:
    write_grades(tmp_path, season())
    found = calibration(tmp_path, "NCAAB", DOCUMENT)
    assert (found["graded_legs"], found["slates"], found["tickets"]) == (200, 10, {"lost": 10})
    assert {row["bucket"] for row in found["tables"]} == {"0–2", "2–4", "4–6", "6–10", "10+"}
    path = write_report("NCAAB", date(2026, 11, 10), DOCUMENT, tmp_path, tmp_path / "reports")
    text = path.read_text("utf-8")
    assert path.name == "v2-evidence-NCAAB-2026-11-10.md"
    for heading in ("## This week", "## Season to date", "## Threshold suggestions",
                    "## By edge bucket and cross-level"):  # fmt: skip
        assert heading in text
    assert "over 4 / under 0 gives 130 legs" in text
    assert path.with_suffix(".json").exists()


# Jobs.


def test_grading_runs_daily_and_the_report_on_mondays() -> None:
    monday = datetime(2026, 11, 30, 8, 0, tzinfo=UTC)  # 09:00 Lagos.
    names = {job.name: job for job in due_jobs(monday, {})}
    assert names["grading"].command[-2:] == ("hoopsedge.v2.grading", "grade")
    assert "weekly_report" in {j.name for j in due_jobs(monday + timedelta(minutes=30), {})}
    tuesday = monday + timedelta(days=1, minutes=30)
    assert "weekly_report" not in {j.name for j in due_jobs(tuesday, {})}


def test_a_saved_document_from_before_b6_gets_the_new_jobs_at_default_times() -> None:
    stored = json.loads(json.dumps(DOCUMENT))
    del stored["schedules"]["grading"], stored["schedules"]["weekly_report"]
    loaded = document_from_stored(stored)
    assert set(loaded.schedules) == set(SCHEDULE_NAMES)
    assert (loaded.schedules["grading"], loaded.schedules["weekly_report"]) == ("09:00", "09:30")


# D101: only the lowest-odds line is ever booked.


def test_the_defaults_book_the_lowest_odds_line_for_every_market() -> None:
    for sport in default_document().sports.values():
        assert (sport.line_mode.totals, sport.line_mode.team_totals) == ("safest", "safest")


def test_main_is_not_a_booking_mode() -> None:
    document = json.loads(json.dumps(DOCUMENT))
    document["sports"]["NBA"]["line_mode"]["team_totals"] = "main"
    with pytest.raises(ValidationError, match="line_mode"):
        ConfigDocument.model_validate(document)


def test_a_document_saved_with_main_reads_as_the_lowest_odds_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stored = json.loads(json.dumps(DOCUMENT))
    stored["sports"]["NCAAB"]["line_mode"]["team_totals"] = "main"
    loaded = document_from_stored(stored)
    assert loaded.sports["NCAAB"].line_mode.team_totals == "safest"
    assert "NCAAB team_totals: stored line mode main reads as safest" in capsys.readouterr().out
