"""Pure logic for SportyBet discovery (docs/01-phase0-feasibility.md section 0.1).

No network or file access here. `scripts/sportybet_discovery.py` does the I/O, and
all market IDs come from markets.yaml.
"""

from collections.abc import Iterable, Sequence
from typing import Any

MAX_TEST_CODES = 5  # Phase 0 booking-test limit.

Selection = dict[str, str]
SelectionKey = tuple[str, str, str | None, str]


class CodeCapError(RuntimeError):
    """Another test booking could exceed the Phase 0 limit."""


def events_from_list(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event for tournament in document["data"]["tournaments"] for event in tournament["events"]
    ]


def is_bookable(market: dict[str, Any]) -> bool:
    outcomes = market.get("outcomes", [])
    return (
        market.get("status") == 0
        and len(outcomes) == 2
        and all(outcome.get("isActive") == 1 for outcome in outcomes)
    )


def _price_gap(market: dict[str, Any]) -> float:
    first, second = market["outcomes"]
    return round(abs(float(first["odds"]) - float(second["odds"])), 4)


def main_line(lines: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """D5: the bookable line with the most even prices.

    Neighbouring lines often tie (1.75/1.90 and 1.90/1.75). A tie goes to SportyBet's
    favourite flag, and otherwise to the first specifier, so the choice is repeatable.
    """
    bookable = [line for line in lines if is_bookable(line)]
    if not bookable:
        return None
    smallest = min(_price_gap(line) for line in bookable)
    tied = sorted(
        (line for line in bookable if _price_gap(line) == smallest),
        key=lambda line: str(line.get("specifier", "")),
    )
    return next((line for line in tied if line.get("favourite") == 1), tied[0])


def _lines_by_market(event: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lines: dict[str, list[dict[str, Any]]] = {}
    for market in event.get("markets", []):
        lines.setdefault(str(market["id"]), []).append(market)
    return lines


def family_coverage(events: Sequence[dict[str, Any]], market_ids: Sequence[str]) -> dict[str, Any]:
    markets: dict[str, dict[str, Any]] = {
        market_id: {
            "events_listed": 0,
            "events_bookable": 0,
            "main_is_favourite": 0,
            "main_not_favourite": 0,
            "favourite_suspended": 0,
            "non_half_lines": 0,
            "lines_listed": 0,
        }
        for market_id in market_ids
    }
    complete = 0
    for event in events:
        by_market = _lines_by_market(event)
        has_all = True
        for market_id in market_ids:
            row = markets[market_id]
            lines = by_market.get(market_id, [])
            row["lines_listed"] += len(lines)
            row["non_half_lines"] += sum(
                not str(line.get("specifier", "")).endswith(".5") for line in lines
            )
            row["events_listed"] += bool(lines)
            chosen = main_line(lines)
            if chosen is None:
                has_all = False
                continue
            row["events_bookable"] += 1
            if chosen.get("favourite") == 1:
                row["main_is_favourite"] += 1
            else:
                row["main_not_favourite"] += 1
            row["favourite_suspended"] += any(
                line.get("favourite") == 1 and not is_bookable(line) for line in lines
            )
        complete += has_all
    return {
        "events": len(events),
        "events_all_markets_bookable": complete,
        "markets": markets,
    }


def handicap_signs_consistent(market: dict[str, Any], outcome_sides: dict[str, str]) -> bool:
    """The specifier is the home handicap: hcp=-1.5 means Home (-1.5), Away (+1.5)."""
    line = float(str(market["specifier"]).split("=", 1)[1])
    expected = {"home": f"({line:+g})", "away": f"({-line:+g})"}
    return all(
        expected[outcome_sides[str(outcome["id"])]] in str(outcome["desc"])
        for outcome in market["outcomes"]
    )


def choose_legs(
    events: Sequence[dict[str, Any]],
    market_ids: Sequence[str],
    count: int,
    earliest_start_ms: int,
) -> list[Selection]:
    """Pick test legs: one per event, not started, rotating through the given markets."""
    legs: list[Selection] = []
    for event in events:
        if len(legs) == count:
            break
        if event.get("status") != 0 or event["estimateStartTime"] < earliest_start_ms:
            continue
        chosen = main_line(_lines_by_market(event).get(market_ids[len(legs) % len(market_ids)], []))
        if chosen is None:
            continue
        leg: Selection = {
            "eventId": str(event["eventId"]),
            "marketId": str(chosen["id"]),
            "outcomeId": str(chosen["outcomes"][0]["id"]),
        }
        if chosen.get("specifier"):
            leg["specifier"] = str(chosen["specifier"])
        legs.append(leg)
    if len(legs) < count:
        raise ValueError(f"Only {len(legs)} of {count} legs available")
    return legs


def selection_key(selection: Selection) -> SelectionKey:
    return (
        selection["eventId"],
        selection["marketId"],
        selection.get("specifier"),
        selection["outcomeId"],
    )


def decoded_selections(document: dict[str, Any]) -> set[SelectionKey]:
    """Read every selection in a share lookup, so unexpected extras can't go unnoticed.

    Selections the site lists under `unavailableOutcomes` aren't in `outcomes`, so a
    comparison reports them as missing.
    """
    return {
        (
            str(event["eventId"]),
            str(market["id"]),
            market.get("specifier") or None,
            str(outcome["id"]),
        )
        for event in document["data"]["outcomes"]
        for market in event["markets"]
        for outcome in market["outcomes"]
    }


def compare_selections(
    intended: Sequence[Selection], document: dict[str, Any]
) -> dict[str, list[SelectionKey]]:
    wanted = {selection_key(selection) for selection in intended}
    found = decoded_selections(document)
    return {
        "missing": sorted(wanted - found, key=str),
        "extra": sorted(found - wanted, key=str),
    }


def assert_code_available(attempts: Sequence[dict[str, Any]]) -> None:
    """Count real codes and unresolved attempts, since a crash may have created a code."""
    used = sum(
        1 for attempt in attempts if attempt.get("code") or attempt.get("outcome") == "pending"
    )
    if used >= MAX_TEST_CODES:
        raise CodeCapError(f"{used} of {MAX_TEST_CODES} test codes already used")
