"""Settlement on every market and edge case, and the grading pass (docs/08 section 7.1, C13)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws

from hoopsedge.common.dynamo import ensure_table
from hoopsedge.engine import Leg, Ticket
from hoopsedge.grading.run import Finals, grade_pass, hand_check_rows
from hoopsedge.grading.settle import Final, settle_leg, settle_ticket
from hoopsedge.pipeline.store import DRY_RUN, PipelineStore

FINAL = Final(home=100, away=90, home_1h=50, away_1h=44)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TIP = NOW - timedelta(hours=10)
SLATE = date(2026, 11, 6)


@pytest.mark.parametrize(
    ("line", "side", "status", "actual"),
    [
        (-5.5, "home", "won", 10),  # Home wins by 10 and covers -5.5.
        (-5.5, "away", "lost", 10),
        (-10.0, "home", "push", 10),  # An integer line, exactly met.
        (-10.0, "away", "push", 10),
        (-10.5, "home", "lost", 10),
        (-10.5, "away", "won", 10),
        (3.5, "home", "won", 10),  # Home getting points.
        (12.5, "away", "lost", 10),
    ],
)
def test_handicap(line: float, side: str, status: str, actual: int) -> None:
    result = settle_leg("handicap", line, None, side, FINAL)
    assert (result.status, result.actual_value) == (status, actual)


@pytest.mark.parametrize(
    ("line", "side", "status"),
    [
        (189.5, "over", "won"),
        (189.5, "under", "lost"),
        (190.0, "over", "push"),
        (190.0, "under", "push"),
        (190.5, "over", "lost"),
        (190.5, "under", "won"),
    ],
)
def test_full_game_total(line: float, side: str, status: str) -> None:
    result = settle_leg("total_ft", line, None, side, FINAL)
    assert (result.status, result.actual_value) == (status, 190)


def test_overtime_counts_because_the_final_score_includes_it() -> None:
    overtime = Final(home=110, away=108, home_1h=50, away_1h=44)
    assert settle_leg("total_ft", 200.5, None, "over", overtime).status == "won"
    assert settle_leg("handicap", -1.5, None, "home", overtime).status == "won"
    assert settle_leg("team_total", 109.5, "home", "over", overtime).status == "won"


@pytest.mark.parametrize(
    ("line", "side", "status"),
    [
        (93.5, "over", "won"),
        (94.0, "over", "push"),
        (94.5, "under", "won"),
        (93.5, "under", "lost"),
    ],
)
def test_first_half_total_uses_only_the_first_half(line: float, side: str, status: str) -> None:
    result = settle_leg("total_1h", line, None, side, FINAL)
    assert (result.status, result.actual_value) == (status, 94)


def test_overtime_cannot_change_a_first_half_result() -> None:
    overtime = Final(home=120, away=118, home_1h=50, away_1h=44)
    assert settle_leg("total_1h", 94.5, None, "under", overtime).status == "won"


@pytest.mark.parametrize(
    ("team", "line", "side", "status", "actual"),
    [
        ("home", 99.5, "over", "won", 100),
        ("home", 100.0, "over", "push", 100),
        ("home", 100.5, "under", "won", 100),
        ("away", 89.5, "over", "won", 90),
        ("away", 90.0, "under", "push", 90),
        ("away", 90.5, "over", "lost", 90),
    ],
)
def test_team_total(team: str, line: float, side: str, status: str, actual: int) -> None:
    result = settle_leg("team_total", line, team, side, FINAL)
    assert (result.status, result.actual_value) == (status, actual)


def test_a_missing_first_half_score_cannot_settle() -> None:
    with pytest.raises(ValueError, match="first-half"):
        settle_leg("total_1h", 94.5, None, "under", Final(100, 90, None, None))


def test_a_full_game_market_does_not_need_the_first_half() -> None:
    assert settle_leg("total_ft", 189.5, None, "over", Final(100, 90, None, None)).status == "won"


def test_tickets_lose_on_any_leg_and_win_only_when_all_are_settled() -> None:
    assert settle_ticket([("won", 2.0), ("lost", 2.0)]) == ("lost", 2.0)
    assert settle_ticket([("lost", 2.0), ("pending", 2.0)]) == ("lost", 1.0)
    assert settle_ticket([("won", 2.0), ("pending", 2.0)]) == ("pending", 2.0)
    assert settle_ticket([("won", 2.0), ("won", 1.5)]) == ("won", 3.0)


def test_pushes_and_voids_settle_at_odds_of_one() -> None:
    assert settle_ticket([("won", 2.0), ("push", 1.9), ("void", 1.8)]) == ("won", 2.0)


# The grading pass.


def _leg(rank: int = 1, game: str = "g1", **changes: Any) -> Leg:
    fields = {
        "rank": rank, "league": "NCAA", "game_id": game, "event_id": f"e-{game}",
        "family": "total_ft", "market_id": "225", "specifier": "total=189.5", "line": 189.5,
        "team": None, "side": "over", "outcome_id": "12", "odds": 2.0, "p_raw": 0.6,
        "p_calibrated": 0.6, "p_win": 0.6, "p_push": 0.0, "p_lose": 0.4, "fair": 0.5, "edge": 0.1,
        "ev": 0.2, "tip_time": TIP, "model_version": "v", "feature_hash": "h",
        "offer_fetched_at": TIP, "predicted_mean": 185.0,
    }  # fmt: skip
    return Leg.model_validate({**fields, **changes})


def _ticket(legs: list[Leg], family: str = "total_ft", number: int = 1) -> Ticket:
    return Ticket.model_validate(
        {
            "league": "NCAA", "family": family, "ticket_no": number, "legs": tuple(legs),
            "combined_odds": 4.0, "combined_prob": 0.36, "expected_value": 0.44,
            "selection_hash": "hash", "passed_gate": True, "edge_label": "measured",
        }
    )  # fmt: skip


class Harness:
    def __init__(self, table: Any) -> None:
        self.table = table
        self.store = PipelineStore(table)
        self.finals: dict[str, Final] = {}
        self.status: dict[str, str] = {}

    def lookup(self) -> Finals:
        return Finals(self.finals, self.status)

    def save(self, name: str, legs: list[Leg], version: int = 1, **changes: Any) -> None:
        self.store.save_ticket(
            name, _ticket(legs, **changes), "run", SLATE, version, DRY_RUN, "DRYRUN-1"
        )

    def grade(self, now: datetime = NOW) -> dict[str, Any]:
        return grade_pass(self.store, self.lookup(), now)

    def leg(self, ticket: str, rank: int = 1) -> dict[str, Any]:
        response = self.table.get_item(Key={"pk": f"TICKET#{ticket}", "sk": f"LEG#{rank:03d}"})
        item: dict[str, Any] = response["Item"]
        return item

    def ticket(self, name: str) -> dict[str, Any]:
        item: dict[str, Any] = self.table.get_item(Key={"pk": f"TICKET#{name}", "sk": "META"})[
            "Item"
        ]
        return item

    def agg(self, sk: str, family: str = "total_ft") -> dict[str, Any] | None:
        response = self.table.get_item(Key={"pk": f"AGG#NCAA#{family}", "sk": sk})
        result: dict[str, Any] | None = response.get("Item")
        return result


@pytest.fixture
def harness() -> Iterator[Harness]:
    with mock_aws():
        resource = boto3.resource(
            "dynamodb", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="x",  # noqa: S106 - Dummy values.
        )  # fmt: skip
        ensure_table(resource, "t")
        yield Harness(resource.Table("t"))


def test_a_final_game_settles_its_leg_and_stores_the_values(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    harness.finals["g1"] = FINAL
    assert harness.grade()["settled"] == 1
    leg = harness.leg("t1")
    assert leg["status"] == "won"
    assert leg["actual_value"] == 190
    assert leg["model_error"] == 5  # 190 actual against 185 predicted.
    assert leg["gsi1pk"] != "PENDINGLEG"
    assert harness.store.pending_legs() == []


def test_an_unfinished_game_stays_pending(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    assert harness.grade()["settled"] == 0
    assert harness.leg("t1")["status"] == "pending"
    assert harness.ticket("t1")["settle_status"] == "pending"


def test_a_ticket_loses_as_soon_as_one_leg_loses(harness: Harness) -> None:
    harness.save("t1", [_leg(1, "g1", side="under"), _leg(2, "g2")])
    harness.finals["g1"] = FINAL
    harness.grade()
    assert harness.ticket("t1")["settle_status"] == "lost"
    assert harness.leg("t1", 2)["status"] == "pending"


def test_a_ticket_wins_once_every_leg_is_settled(harness: Harness) -> None:
    harness.save("t1", [_leg(1, "g1"), _leg(2, "g2")])
    harness.finals["g1"] = FINAL
    harness.grade()
    assert harness.ticket("t1")["settle_status"] == "pending"
    harness.finals["g2"] = FINAL
    harness.grade()
    ticket = harness.ticket("t1")
    assert ticket["settle_status"] == "won"
    assert ticket["settled_combined_odds"] == 4


def test_a_push_leaves_the_ticket_alive_at_lower_odds(harness: Harness) -> None:
    harness.save(
        "t1",
        [_leg(1, "g1", line=190.0, specifier="total=190.0"), _leg(2, "g2")],
    )
    harness.finals.update(g1=FINAL, g2=FINAL)
    harness.grade()
    assert harness.leg("t1")["status"] == "push"
    ticket = harness.ticket("t1")
    assert ticket["settle_status"] == "won"
    assert ticket["settled_combined_odds"] == 2


def test_grading_twice_changes_nothing(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    harness.finals["g1"] = FINAL
    harness.grade()
    first = harness.leg("t1")
    second_pass = harness.grade(NOW + timedelta(hours=1))
    assert second_pass["settled"] == 0
    assert second_pass["corrections"] == 0
    assert harness.leg("t1") == first


# Postponements.


@pytest.mark.parametrize("status", ["STATUS_POSTPONED", "STATUS_CANCELED"])
def test_a_game_not_played_36_hours_after_tip_is_void(harness: Harness, status: str) -> None:
    harness.save("t1", [_leg(1, "g1"), _leg(2, "g2")])
    harness.status["g1"] = status
    harness.finals["g2"] = FINAL
    harness.grade(TIP + timedelta(hours=35))
    assert harness.leg("t1")["status"] == "pending"
    harness.grade(TIP + timedelta(hours=37))
    leg = harness.leg("t1")
    assert leg["status"] == "void"
    assert leg.get("actual_value") is None
    assert harness.ticket("t1")["settle_status"] == "won"
    assert harness.ticket("t1")["settled_combined_odds"] == 2


def test_a_postponed_game_played_within_36_hours_settles_normally(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    harness.status["g1"] = "STATUS_POSTPONED"
    harness.finals["g1"] = FINAL
    harness.grade(TIP + timedelta(hours=20))
    assert harness.leg("t1")["status"] == "won"


def test_a_late_game_with_no_postponement_is_never_voided(harness: Harness) -> None:
    # Missing data isn't a postponement: a stuck ingest must not void real bets.
    harness.save("t1", [_leg()])
    harness.status["g1"] = "STATUS_SCHEDULED"
    summary = harness.grade(TIP + timedelta(hours=72))
    assert harness.leg("t1")["status"] == "pending"
    assert summary["overdue"] == 1
    alerts = harness.table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq("ALERT")
    )["Items"]
    assert any("overdue" in alert["message"] for alert in alerts)


def test_a_missing_first_half_score_leaves_the_leg_pending_and_visible(harness: Harness) -> None:
    harness.save("t1", [_leg(family="total_1h", market_id="68", line=94.5)], family="total_1h")
    harness.finals["g1"] = Final(100, 90, None, None)
    summary = harness.grade()
    assert harness.leg("t1")["status"] == "pending"
    assert summary["failures"] == ["g1"]


# Score corrections.


def test_a_score_correction_regrades_flags_and_alerts(harness: Harness) -> None:
    harness.save("t1", [_leg(1, "g1", line=189.5)])
    harness.finals["g1"] = FINAL
    harness.grade()
    assert harness.leg("t1")["status"] == "won"
    harness.finals["g1"] = Final(95, 90, 50, 44)  # 185: the over at 189.5 now loses.
    summary = harness.grade(NOW + timedelta(hours=1))
    assert summary["corrections"] == 1
    leg = harness.leg("t1")
    assert leg["status"] == "lost"
    assert leg["actual_value"] == 185
    assert leg["correction"] is True
    ticket = harness.ticket("t1")
    assert ticket["settle_status"] == "lost"
    assert ticket["correction"] is True
    alerts = harness.table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq("ALERT")
    )["Items"]
    assert any(alert["severity"] == "WARNING" for alert in alerts)


def test_a_first_half_correction_only_matters_to_first_half_legs(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    harness.finals["g1"] = FINAL
    harness.grade()
    harness.finals["g1"] = Final(100, 90, 40, 40)
    assert harness.grade()["corrections"] == 0
    assert harness.leg("t1").get("correction") is None


# Aggregates (C13).


def test_aggregates_count_wins_losses_and_calibration_buckets(harness: Harness) -> None:
    harness.save(
        "t1",
        [
            _leg(1, "g1", p_calibrated=0.62),
            _leg(2, "g2", side="under", p_calibrated=0.61),
            _leg(3, "g3", line=190.0, specifier="total=190.0", p_calibrated=0.71),
        ],
    )
    harness.finals.update(g1=FINAL, g2=FINAL, g3=FINAL)
    harness.grade()
    for key in (f"D#{SLATE}", "ALL"):
        item = harness.agg(key)
        assert item is not None
        assert (item["legs"], item["won"], item["lost"], item["push"]) == (3, 1, 1, 1)
        assert item["buckets"]["0.60"] == {"legs": 2, "won": 1}
        assert item["buckets"]["0.70"] == {"legs": 0, "won": 0}  # A push settles nothing.


def test_superseded_tickets_are_graded_but_never_counted(harness: Harness) -> None:
    harness.save("old", [_leg()], version=1)
    harness.save("new", [_leg()], version=2)
    harness.store.supersede("NCAA", SLATE, 2)
    harness.finals["g1"] = FINAL
    harness.grade()
    assert harness.leg("old")["status"] == "won"  # Every leg is graded.
    assert harness.leg("new")["status"] == "won"
    item = harness.agg("ALL")
    assert item is not None
    assert item["legs"] == 1  # But the re-run can't inflate the record.


def test_a_correction_updates_the_aggregates(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    harness.finals["g1"] = FINAL
    harness.grade()
    harness.finals["g1"] = Final(95, 90, 50, 44)
    harness.grade(NOW + timedelta(hours=1))
    item = harness.agg("ALL")
    assert item is not None
    assert (item["won"], item["lost"]) == (0, 1)


def test_markets_keep_separate_aggregates(harness: Harness) -> None:
    harness.save("t1", [_leg()])
    half = _leg(family="total_1h", market_id="68", specifier="total=93.5", line=93.5)
    harness.save("t2", [half], family="total_1h")
    harness.finals["g1"] = FINAL
    harness.grade()
    assert harness.agg("ALL") is not None
    assert harness.agg("ALL", "total_1h") is not None


def test_nothing_to_grade_is_a_quiet_pass(harness: Harness) -> None:
    assert grade_pass(harness.store, Finals({}, {}), NOW)["settled"] == 0


def test_the_hand_check_lists_real_names_across_markets_without_repeats(harness: Harness) -> None:
    harness.save("t1", [_leg(1, "g1"), _leg(2, "g2")])
    harness.save("t1b", [_leg(1, "g1")], version=2)  # A re-run of the same leg.
    half = _leg(family="total_1h", market_id="68", specifier="total=93.5", line=93.5)
    harness.save("t2", [half], family="total_1h")
    harness.finals.update(g1=FINAL, g2=FINAL)
    harness.grade()
    legs = harness.store.settled_legs("NCAA", NOW - timedelta(days=5))
    rows = hand_check_rows(
        legs, 2, {"g1": ("h", "a")}, {("NCAA", "h"): "Duke Blue Devils", ("NCAA", "a"): "UNC"}
    )
    assert len(rows) == 2
    assert {row.split(" | ")[2].split()[0] for row in rows} == {"total_ft", "total_1h"}
    everything = hand_check_rows(legs, 20, {}, {})
    assert len(everything) == 3
    named = hand_check_rows(
        legs, 20, {"g1": ("h", "a")}, {("NCAA", "h"): "Duke", ("NCAA", "a"): "UNC"}
    )
    assert any("Duke vs UNC" in row and "actual 190" in row and "won" in row for row in named)
