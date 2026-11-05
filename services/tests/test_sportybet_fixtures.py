"""Checks the recorded SportyBet responses against markets.yaml and the discovery logic."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from hoopsedge.sportybet.discovery import (
    compare_selections,
    decoded_selections,
    events_from_list,
    handicap_signs_consistent,
    main_line,
)

SERVICES = Path(__file__).resolve().parent.parent
FIXTURES = SERVICES / "tests" / "fixtures" / "sportybet"
PACKAGE = SERVICES / "hoopsedge" / "sportybet"
LEDGER = SERVICES.parent / "docs" / "reports" / "phase0-sportybet-test-codes.json"
MARKETS: dict[str, Any] = yaml.safe_load((PACKAGE / "markets.yaml").read_text("utf-8"))
ENDPOINTS: dict[str, Any] = yaml.safe_load((PACKAGE / "endpoints.yaml").read_text("utf-8"))
SUCCESS = ENDPOINTS["success_biz_code"]
FAMILIES = ("handicap", "total_ft", "total_1h", "team_total")
BOOKED = ("two-leg", "all-markets", "twenty-legs", "same-event")


def fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def ledger(name: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = json.loads(LEDGER.read_text("utf-8"))
    return next(attempt for attempt in attempts if attempt["name"] == name)


def market_ids(family: str) -> list[str]:
    return [str(market["market_id"]) for market in MARKETS["families"][family]["markets"]]


def listed_events() -> list[dict[str, Any]]:
    names = ("events-nba-page1.json", "events-ncaa-page1.json", "events-ncaa-page2.json")
    return [event for name in names for event in events_from_list(fixture(name))]


def test_markets_file_defines_all_four_families_for_both_leagues() -> None:
    assert set(MARKETS["families"]) == set(FAMILIES)
    assert set(MARKETS["leagues"]) == {"NBA", "NCAA"}


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_appears_in_real_responses_with_its_outcomes(family: str) -> None:
    allowed = {str(outcome) for outcome in MARKETS["families"][family]["outcomes"]}
    key = MARKETS["families"][family]["specifier_key"]
    for market_id in market_ids(family):
        lines = [
            market
            for event in listed_events()
            for market in event["markets"]
            if str(market["id"]) == market_id
        ]
        assert lines, market_id
        for line in lines:
            assert str(line["specifier"]).startswith(f"{key}=")
            assert {str(outcome["id"]) for outcome in line["outcomes"]} == allowed


def test_handicap_outcomes_follow_the_home_specifier_in_real_data() -> None:
    family = MARKETS["families"]["handicap"]
    sides = {str(key): value for key, value in family["outcomes"].items()}
    lines = [
        market
        for event in listed_events()
        for market in event["markets"]
        if str(market["id"]) in market_ids("handicap")
    ]
    assert lines
    assert all(handicap_signs_consistent(line, sides) for line in lines)


def test_main_line_exists_for_the_full_game_total_of_a_tonight_event() -> None:
    detail = fixture("event-detail-nba-0.json")["data"]
    lines = [m for m in detail["markets"] if str(m["id"]) in market_ids("total_ft")]
    chosen = main_line(lines)
    assert chosen is not None
    assert chosen["status"] == 0


@pytest.mark.parametrize("name", BOOKED)
def test_real_lookup_matches_what_was_booked(name: str) -> None:
    attempt = ledger(name)
    lookup = fixture(f"share-lookup-{name}.json")
    created = fixture(f"share-create-{name}.json")
    assert lookup["bizCode"] == created["bizCode"] == SUCCESS
    assert lookup["data"]["shareCode"] == created["data"]["shareCode"] == attempt["code"]
    assert compare_selections(attempt["legs"], lookup) == {"missing": [], "extra": []}
    assert lookup["data"]["unavailableOutcomes"] == []


def test_a_changed_line_in_a_real_lookup_is_detected() -> None:
    attempt = ledger("two-leg")
    lookup = fixture("share-lookup-two-leg.json")
    lookup["data"]["outcomes"][0]["markets"][0]["specifier"] = "hcp=-99.5"
    difference = compare_selections(attempt["legs"], lookup)
    assert len(difference["missing"]) == 1
    assert len(difference["extra"]) == 1


def test_twenty_leg_code_holds_twenty_distinct_events() -> None:
    keys = decoded_selections(fixture("share-lookup-twenty-legs.json"))
    assert len(keys) == 20
    assert len({key[0] for key in keys}) == 20


def test_same_event_code_was_accepted_so_d10_is_ours_to_enforce() -> None:
    keys = decoded_selections(fixture("share-lookup-same-event.json"))
    assert len({key[0] for key in keys}) == 1
    assert len(keys) == 2
    assert MARKETS["booking"]["same_event_rejected"] is False


def test_rejections_are_http_200_with_a_failing_biz_code() -> None:
    attempt = ledger("bad-outcome")
    assert attempt["http_status"] == 200
    assert attempt["code"] is None
    for name in ("share-create-bad-outcome.json", "share-lookup-invalid.json"):
        document = fixture(name)
        assert document["bizCode"] == ENDPOINTS["share_lookup"]["invalid_code_biz_code"]
        assert "data" not in document


def test_fixtures_hold_no_user_identity() -> None:
    for path in FIXTURES.glob("share-*.json"):
        data = json.loads(path.read_text("utf-8")).get("data", {})
        assert not data.get("userId"), path.name
