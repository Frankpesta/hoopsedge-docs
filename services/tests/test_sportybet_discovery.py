"""Discovery logic: main-line rule, coverage, leg choice, verification and the code cap."""

from typing import Any

import pytest

from hoopsedge.sportybet.discovery import (
    MAX_TEST_CODES,
    CodeCapError,
    assert_code_available,
    choose_legs,
    compare_selections,
    decoded_selections,
    events_from_list,
    family_coverage,
    handicap_signs_consistent,
    main_line,
    selection_key,
)


def market(
    market_id: str,
    line: str | None,
    prices: tuple[str, str] = ("1.90", "1.90"),
    status: int = 0,
    favourite: int = 0,
    active: tuple[int, int] = (1, 1),
    descs: tuple[str, str] = ("Over", "Under"),
    ids: tuple[str, str] = ("12", "13"),
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": market_id,
        "status": status,
        "favourite": favourite,
        "outcomes": [
            {"id": ids[0], "odds": prices[0], "isActive": active[0], "desc": descs[0]},
            {"id": ids[1], "odds": prices[1], "isActive": active[1], "desc": descs[1]},
        ],
    }
    if line is not None:
        document["specifier"] = line
    return document


def event(event_id: str, markets: list[dict[str, Any]], start: int = 10_000_000) -> dict[str, Any]:
    return {"eventId": event_id, "estimateStartTime": start, "status": 0, "markets": markets}


def test_main_line_is_the_most_balanced_price() -> None:
    lines = [
        market("225", "total=130.5", ("1.40", "2.80")),
        market("225", "total=135.5", ("1.88", "1.92")),
        market("225", "total=140.5", ("2.70", "1.42")),
    ]
    chosen = main_line(lines)
    assert chosen is not None
    assert chosen["specifier"] == "total=135.5"


def test_main_line_ignores_suspended_and_inactive_lines() -> None:
    lines = [
        market("225", "total=135.5", ("1.90", "1.90"), status=2, favourite=1),
        market("225", "total=136.5", ("1.90", "1.90"), active=(1, 0)),
        market("225", "total=140.5", ("1.60", "2.30")),
    ]
    chosen = main_line(lines)
    assert chosen is not None
    assert chosen["specifier"] == "total=140.5"


def test_main_line_tie_prefers_the_favourite_flag() -> None:
    lines = [
        market("227", "total=76.5", ("1.75", "1.90")),
        market("227", "total=77.5", ("1.90", "1.75"), favourite=1),
    ]
    chosen = main_line(lines)
    assert chosen is not None
    assert chosen["specifier"] == "total=77.5"


def test_main_line_tie_without_favourite_is_deterministic() -> None:
    first = market("227", "total=76.5", ("1.75", "1.90"))
    second = market("227", "total=77.5", ("1.90", "1.75"))
    one = main_line([first, second])
    two = main_line([second, first])
    assert one is not None and two is not None
    assert one["specifier"] == two["specifier"] == "total=76.5"


def test_main_line_is_none_when_nothing_is_bookable() -> None:
    assert main_line([market("225", "total=135.5", status=2)]) is None
    assert main_line([]) is None


def test_events_from_list_flattens_tournaments() -> None:
    document = {
        "data": {"tournaments": [{"events": [{"eventId": "a"}]}, {"events": [{"eventId": "b"}]}]}
    }
    assert [e["eventId"] for e in events_from_list(document)] == ["a", "b"]


def test_family_coverage_counts_each_market_and_full_events() -> None:
    full = event(
        "e1",
        [
            market("225", "total=135.5", favourite=1),
            market("68", "total=65.5", favourite=1),
        ],
    )
    suspended_half = event(
        "e2",
        [
            market("225", "total=140.5", ("1.60", "2.30"), favourite=1),
            market("225", "total=142.5", ("1.90", "1.90")),
            market("68", "total=70.5", status=2, favourite=1),
        ],
    )
    summary = family_coverage([full, suspended_half], ["225", "68"])
    assert summary["events"] == 2
    assert summary["events_all_markets_bookable"] == 1
    assert summary["markets"]["225"]["events_listed"] == 2
    assert summary["markets"]["225"]["events_bookable"] == 2
    assert summary["markets"]["225"]["main_is_favourite"] == 1
    assert summary["markets"]["225"]["main_not_favourite"] == 1
    assert summary["markets"]["68"]["events_listed"] == 2
    assert summary["markets"]["68"]["events_bookable"] == 1


def test_handicap_signs_match_the_specifier() -> None:
    outcomes = {"1714": "home", "1715": "away"}
    good = market("223", "hcp=-1.5", descs=("Home (-1.5)", "Away (+1.5)"), ids=("1714", "1715"))
    bad = market("223", "hcp=-1.5", descs=("Home (+1.5)", "Away (-1.5)"), ids=("1714", "1715"))
    assert handicap_signs_consistent(good, outcomes) is True
    assert handicap_signs_consistent(bad, outcomes) is False


def test_choose_legs_uses_one_leg_per_event_and_rotates_markets() -> None:
    events = [
        event(f"e{i}", [market("225", "total=135.5"), market("68", "total=65.5")]) for i in range(3)
    ]
    legs = choose_legs(events, ["225", "68"], count=3, earliest_start_ms=0)
    assert [leg["eventId"] for leg in legs] == ["e0", "e1", "e2"]
    assert [leg["marketId"] for leg in legs] == ["225", "68", "225"]
    assert legs[0] == {
        "eventId": "e0",
        "marketId": "225",
        "specifier": "total=135.5",
        "outcomeId": "12",
    }


def test_choose_legs_skips_started_and_soon_events() -> None:
    soon = event("soon", [market("225", "total=135.5")], start=999)
    started = {**event("live", [market("225", "total=135.5")]), "status": 1}
    later = event("later", [market("225", "total=135.5")])
    legs = choose_legs([soon, started, later], ["225"], count=1, earliest_start_ms=1000)
    assert [leg["eventId"] for leg in legs] == ["later"]


def test_choose_legs_refuses_a_short_ticket() -> None:
    with pytest.raises(ValueError, match="Only 1 of 2"):
        choose_legs([event("e", [market("225", "total=135.5")])], ["225"], 2, 0)


def decoded(*legs: tuple[str, str, str | None, str]) -> dict[str, Any]:
    outcomes = []
    for event_id, market_id, specifier, outcome_id in legs:
        found: dict[str, Any] = {"id": market_id, "outcomes": [{"id": outcome_id}]}
        if specifier is not None:
            found["specifier"] = specifier
        outcomes.append({"eventId": event_id, "markets": [found]})
    return {"data": {"outcomes": outcomes}}


def test_decoded_selections_read_the_share_lookup_shape() -> None:
    document = decoded(("e1", "225", "total=135.5", "12"), ("e2", "1", None, "1"))
    assert decoded_selections(document) == {
        ("e1", "225", "total=135.5", "12"),
        ("e2", "1", None, "1"),
    }


def test_decoded_selections_read_every_market_and_outcome() -> None:
    document = decoded(("e1", "225", "total=135.5", "12"))
    document["data"]["outcomes"][0]["markets"].append(
        {"id": "68", "specifier": "total=65.5", "outcomes": [{"id": "12"}, {"id": "13"}]}
    )
    assert decoded_selections(document) == {
        ("e1", "225", "total=135.5", "12"),
        ("e1", "68", "total=65.5", "12"),
        ("e1", "68", "total=65.5", "13"),
    }


def test_compare_selections_passes_only_on_an_exact_match() -> None:
    wanted = [{"eventId": "e1", "marketId": "225", "specifier": "total=135.5", "outcomeId": "12"}]
    assert selection_key(wanted[0]) == ("e1", "225", "total=135.5", "12")
    same = decoded(("e1", "225", "total=135.5", "12"))
    other_line = decoded(("e1", "225", "total=136.5", "12"))
    assert compare_selections(wanted, same) == {"missing": [], "extra": []}
    assert compare_selections(wanted, other_line) == {
        "missing": [("e1", "225", "total=135.5", "12")],
        "extra": [("e1", "225", "total=136.5", "12")],
    }


def test_code_cap_counts_codes_and_unresolved_attempts() -> None:
    attempts: list[dict[str, Any]] = [
        {"name": str(i), "code": "X"} for i in range(MAX_TEST_CODES - 1)
    ]
    assert_code_available([*attempts, {"name": "rejected", "code": None, "outcome": "rejected"}])
    with pytest.raises(CodeCapError):
        assert_code_available([*attempts, {"name": "crashed", "code": None, "outcome": "pending"}])
    with pytest.raises(CodeCapError):
        assert_code_available([*attempts, {"name": "last", "code": "Y"}])
