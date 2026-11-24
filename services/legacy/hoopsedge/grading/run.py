"""The grading pass (docs/08 section 7.1, C13). Safe to run as often as you like.

    docker compose run --rm pipeline python -m hoopsedge.grading.run

Every leg is graded, including superseded ones, but `AGG#` counts only the latest versions. A
settled leg is never touched again unless the numbers it settled on have changed.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from hoopsedge.common.aggregates import merge_aggregates
from hoopsedge.common.dynamo import connect, ensure_table, table_name
from hoopsedge.grading.data import load_finals, load_game_teams, load_statuses, team_names
from hoopsedge.grading.settle import (
    LOST,
    PUSH,
    VOID,
    WON,
    Final,
    settle_leg,
    settle_ticket,
)
from hoopsedge.pipeline.store import SUPERSEDED, WARNING, PipelineStore

VOID_AFTER = timedelta(hours=36)  # Docs/08's configurable rule. SportyBet's own isn't recorded.
NOT_PLAYED = frozenset({"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"})
CORRECTION_WINDOW = timedelta(days=14)  # As far back as live ingest can notice a correction.
BUCKET = 0.05
DEFAULT_DATA_DIR = "/workspace/data"


@dataclass(frozen=True)
class Finals:
    games: dict[str, Final]
    status: dict[str, str]


def _bucket(p: float) -> str:
    return f"{int(p / BUCKET + 1e-9) * BUCKET:.2f}"


def _float(value: Any) -> float:
    return float(value)


def _team(leg: dict[str, Any]) -> str | None:
    return None if leg.get("team") is None else str(leg["team"])


def _settled_on(leg: dict[str, Any], final: Final) -> dict[str, int]:
    """Only the numbers this leg depends on, so a first-half fix leaves a full-game leg alone."""
    if leg["family"] == "total_1h":
        if final.home_1h is None or final.away_1h is None:
            raise ValueError("No first-half score to settle on")
        return {"home": final.home_1h, "away": final.away_1h}
    return {"home": final.home, "away": final.away}


def _fields(leg: dict[str, Any], final: Final) -> dict[str, Any]:
    result = settle_leg(
        str(leg["family"]), _float(leg["line"]), _team(leg), str(leg["side"]), final
    )
    predicted = leg.get("predicted_mean")
    return {
        "status": result.status,
        "actual_value": result.actual_value,
        "model_error": None if predicted is None else result.actual_value - _float(predicted),
        "settled_on": _settled_on(leg, final),
    }


def _ticket_id(leg: dict[str, Any]) -> str:
    return str(leg["pk"]).split("#", 1)[1]


def _settle_tickets(store: PipelineStore, tickets: set[str], flagged: set[str]) -> None:
    for ticket_id in sorted(tickets):
        legs = store.ticket_legs(ticket_id)
        status, odds = settle_ticket((str(leg["status"]), _float(leg["odds"])) for leg in legs)
        store.update_ticket_settlement(ticket_id, status, odds, ticket_id in flagged)


def _aggregate(legs: list[dict[str, Any]], store: PipelineStore) -> dict[str, Any]:
    """Docs/09's track record: hit rate, flat-stake return, calibration and near misses."""
    counts: dict[str, Any] = {"legs": len(legs), WON: 0, LOST: 0, PUSH: 0, VOID: 0}
    buckets: dict[str, dict[str, float]] = {}
    profit = breakeven = 0.0
    for leg in legs:
        status = str(leg["status"])
        counts[status] += 1
        odds = _float(leg["odds"])
        name = _bucket(_float(leg["p_calibrated"]))
        bucket = buckets.setdefault(name, {"legs": 0, "won": 0, "p_sum": 0.0})
        if status in {WON, LOST}:  # A push or void settles nothing, so it can't test a probability.
            bucket["legs"] += 1
            bucket["won"] += status == WON
            bucket["p_sum"] += _float(leg["p_calibrated"])
            profit += odds - 1.0 if status == WON else -1.0
            breakeven += 1.0 / odds
    tickets = {"won": 0, "lost": 0, "pending": 0}
    misses: dict[str, int] = {}
    legs_won_on_lost = legs_on_lost = 0
    for ticket_id in sorted({_ticket_id(leg) for leg in legs}):
        status = str(store.ticket_meta(ticket_id).get("settle_status", "pending"))
        tickets[status] = tickets.get(status, 0) + 1
        if status == LOST:
            statuses = [str(item["status"]) for item in store.ticket_legs(ticket_id)]
            missed = str(statuses.count(LOST))
            misses[missed] = misses.get(missed, 0) + 1
            legs_won_on_lost += statuses.count(WON)
            legs_on_lost += len(statuses)
    return {
        **counts,
        "buckets": buckets,
        "profit": profit,
        "breakeven_sum": breakeven,
        "tickets": tickets,
        "missed_by": misses,
        "legs_won_on_lost": legs_won_on_lost,
        "legs_on_lost": legs_on_lost,
    }


def _merge(items: list[dict[str, Any]]) -> dict[str, Any]:
    return merge_aggregates(items)


def _slate_of(store: PipelineStore, leg: dict[str, Any]) -> str:
    """Legs stored before Phase 7 hold no slate date, but their ticket always has one."""
    return str(leg.get("slate_date") or store.ticket_meta(_ticket_id(leg))["slate_date"])


def _refresh_aggregates(store: PipelineStore, touched: set[tuple[str, str, str]]) -> None:
    """Rebuilt from settled legs, so a correction or a re-run can't drift the record (C13)."""
    for league, family, slate in sorted(touched):
        day = date.fromisoformat(slate)
        since = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=1)
        legs = [
            leg
            for leg in store.settled_legs(league, since)
            if leg["family"] == family
            and _slate_of(store, leg) == slate
            and store.ticket_meta(_ticket_id(leg)).get("status") != SUPERSEDED
        ]
        store.save_aggregate(league, family, f"D#{slate}", _aggregate(legs, store))
        days = store.daily_aggregates(league, family)
        store.save_aggregate(league, family, "ALL", _merge(days))


def grade_pass(store: PipelineStore, finals: Finals, now: datetime) -> dict[str, Any]:
    summary: dict[str, Any] = {"settled": 0, "voided": 0, "corrections": 0, "overdue": 0}
    failures: list[str] = []
    tickets: set[str] = set()
    flagged: set[str] = set()
    touched: set[tuple[str, str, str]] = set()

    def note(leg: dict[str, Any]) -> None:
        tickets.add(_ticket_id(leg))
        touched.add((str(leg["league"]), str(leg["family"]), _slate_of(store, leg)))

    for leg in store.pending_legs():
        game_id = str(leg["game_id"])
        final = finals.games.get(game_id)
        tip = datetime.fromisoformat(str(leg["tip_time"]))
        if final is not None:
            try:
                fields = _fields(leg, final)
            except ValueError:
                failures.append(game_id)
                continue
            store.settle_leg(leg, fields, now)
            summary["settled"] += 1
            note(leg)
        elif now - tip > VOID_AFTER:
            if finals.status.get(game_id) in NOT_PLAYED:
                store.settle_leg(leg, {"status": VOID, "actual_value": None}, now)
                summary["voided"] += 1
                note(leg)
            else:
                summary["overdue"] += 1
                # Missing data isn't a postponement. Voiding here could hide a real result.
                store.alert(
                    now, WARNING, f"grading-overdue-{game_id}",
                    f"Game {game_id} is overdue with no final", "grading",
                )  # fmt: skip

    for leg in store.settled_legs_since_all(now - CORRECTION_WINDOW):
        final = finals.games.get(str(leg["game_id"]))
        if final is None or leg.get("settled_on") is None:
            continue
        try:
            fields = _fields(leg, final)
        except ValueError:
            continue
        stored = {name: int(value) for name, value in leg["settled_on"].items()}
        if fields["settled_on"] != stored:
            store.settle_leg(leg, {**fields, "correction": True}, now)
            summary["corrections"] += 1
            note(leg)
            flagged.add(_ticket_id(leg))
            store.alert(
                now, WARNING, f"score-correction-{leg['game_id']}",
                f"Score corrected for {leg['game_id']}: {stored} to {fields['settled_on']}",
                _ticket_id(leg),
            )  # fmt: skip

    _settle_tickets(store, tickets, flagged)
    _refresh_aggregates(store, touched)
    summary["failures"] = sorted(set(failures))
    return summary


def hand_check_rows(
    legs: list[dict[str, Any]],
    count: int,
    game_teams: dict[str, tuple[str, str]],
    names: dict[tuple[str, str], str],
) -> list[str]:
    """Plain lines to check against the real results: docs/08's 20 legs, across every market."""
    by_family: dict[str, list[dict[str, Any]]] = {}
    newest_first = sorted(legs, key=lambda leg: (str(leg["gsi1sk"]), str(leg["sk"])), reverse=True)
    seen: set[tuple[str, str, str, str]] = set()
    for leg in newest_first:
        # Re-runs repeat the same leg, and one check of it is enough.
        key = (str(leg["game_id"]), str(leg["family"]), str(leg["specifier"]), str(leg["side"]))
        if key not in seen:
            seen.add(key)
            by_family.setdefault(str(leg["family"]), []).append(leg)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < count and any(by_family.values()):
        for family in sorted(by_family):
            if by_family[family] and len(chosen) < count:
                chosen.append(by_family[family].pop(0))
    rows: list[str] = []
    for leg in chosen:
        league = str(leg["league"])
        home, away = game_teams.get(str(leg["game_id"]), ("?", "?"))
        match = f"{names.get((league, home), home)} vs {names.get((league, away), away)}"
        team = f" {leg['team']}" if leg.get("team") else ""
        rows.append(
            f"{league} | {match} | {leg['family']}{team} {leg['side']} line {leg['line']} | "
            f"actual {leg.get('actual_value')} | {leg['status']}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["grade", "hand-check"], default="grade")
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    resource = connect()
    ensure_table(resource, table_name())
    store = PipelineStore(resource.Table(table_name()))
    now = datetime.now(UTC)
    today: date = now.date()
    if args.command == "hand-check":
        game_teams: dict[str, tuple[str, str]] = {}
        for league in ("NBA", "NCAA"):
            game_teams.update(load_game_teams(data_dir, league, today))
        legs = store.settled_legs_since_all(now - timedelta(days=30))
        print("\n".join(hand_check_rows(legs, args.count, game_teams, team_names(data_dir))))
        return 0
    games: dict[str, Final] = {}
    for league in ("NBA", "NCAA"):
        games.update(load_finals(data_dir, league, today))
    print(grade_pass(store, Finals(games, load_statuses(store)), now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
