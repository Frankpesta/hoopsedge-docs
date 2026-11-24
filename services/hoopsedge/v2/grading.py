"""Grading and calibration for v2 (brief §9, D90). Nothing here changes the config.

    docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.grading grade
    ... report --sport NCAAB [--week-ending 2026-11-30]

**Grading.** Every game of a slate is graded from the newest full run that started before its
kickoff, so the lines are the last ones seen before the game. Each game gives graded legs for both
`l5_same_level_only` variants (the run stores both predictions):

- totals, in the direction of the model's edge against the main line, at the main line and at the
  `safest` line (lowest for an Over, highest for an Under), whatever the thresholds said;
- team totals, on the side with the larger edge, at the team's main line.

Legs are graded whether or not they cleared a threshold, so any threshold can be judged
afterwards. Every ticket leg is graded on its booked line too, and each ticket gets a result.
Grades are files, `data/v2/grades/{sport}/{date}.json`, rewritten until nothing is pending.

**Calibration (D90).** Hit rate is judged against the bookmaker-implied rate (the mean of 1/odds),
and on flat-stake ROI (1 unit per leg, a push returns the stake). Suggestions are made per
sport × market × line mode, for the variant in force, once at least 100 graded legs clear the
suggested threshold and they average at least `min_legs` per slate. The other splits (edge
bucket, cross-level, the other variant) are read-only tables. The owner applies a suggestion;
nothing here does (brief §9).
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from hoopsedge.grading.settle import LOST, PENDING, PUSH, VOID, WON, Final, settle_leg
from hoopsedge.l5.model import team_pick
from hoopsedge.v2.views import REBOOK, grades_path, grades_root, runs_root

EPS = 1e-9
MIN_GRADED = 100  # Brief §9: a bucket needs this many graded legs before it suggests anything.
BUCKETS: tuple[tuple[float, float | None], ...] = ((0, 2), (2, 4), (4, 6), (6, 10), (10, None))
THRESHOLDS = tuple(step / 2 for step in range(0, 21))  # 0 to 10 in halves.
VOID_STATUSES = frozenset({"STATUS_CANCELED", "STATUS_POSTPONED"})
TOTALS = "totals"
TEAM_TOTAL = "team_total"
MARKETS = (TOTALS, TEAM_TOTAL)
MODES = {TOTALS: ("main", "safest"), TEAM_TOTAL: ("main",)}
REGRADE_DAYS = 14  # A slate still pending after this long is left as it is.
DEFAULT_DATA_DIR = "/workspace/data"
DEFAULT_REPORT_DIR = "/workspace/docs/reports"  # Where v1 wrote its evidence (brief §9).


@dataclass(frozen=True)
class Outcome:
    """A game's state for grading: a final score, void, or not yet."""

    status: str  # WON is never used here: "final", VOID or PENDING.
    home: int | None = None
    away: int | None = None


FINAL = "final"


@dataclass(frozen=True)
class GradedLeg:
    sport: str
    slate: str
    run_id: str
    game_id: str
    event_id: str
    market: str
    same_level: bool  # Which `l5_same_level_only` variant predicted it.
    active: bool  # The variant the run booked with.
    mode: str
    team: str | None
    direction: str
    line: float
    odds: float
    edge: float  # Projection minus the main line, signed.
    cross_level: bool
    result: str
    actual: int | None

    @property
    def profit(self) -> float:
        """Flat stake of 1 unit. A push gives the stake back."""
        if self.result == WON:
            return self.odds - 1
        return -1.0 if self.result == LOST else 0.0


def grade(
    market: str, line: float, team: str | None, direction: str, outcome: Outcome
) -> tuple[str, int | None]:
    if outcome.status != FINAL or outcome.home is None or outcome.away is None:
        return outcome.status, None
    family = "total_ft" if market == TOTALS else "team_total"
    final = Final(outcome.home, outcome.away, None, None)
    settled = settle_leg(family, line, team, direction.lower(), final)
    return settled.status, settled.actual_value


def outcomes_from(results: Iterable[Mapping[str, Any]]) -> dict[str, Outcome]:
    """ESPN results by game id. Cancelled and postponed games are void."""
    out = {}
    for item in results:
        if item["completed"]:
            out[str(item["game_id"])] = Outcome(FINAL, int(item["home_pts"]), int(item["away_pts"]))
        elif item["status"] in VOID_STATUSES:
            out[str(item["game_id"])] = Outcome(VOID)
    return out


def _cross(flags: Iterable[str]) -> bool:
    return any(flag.startswith("cross_level") for flag in flags)


def row_legs(run: Mapping[str, Any], row: Mapping[str, Any], outcome: Outcome) -> list[GradedLeg]:
    """Every graded leg one game gives, for both variants (module docstring)."""
    sport = run["sport"]
    active_same_level = bool(run["config"]["sports"][sport]["l5_same_level_only"])
    legs = row.get("legs") or {}
    team_lines = row.get("team_lines") or {}
    out: list[GradedLeg] = []

    def add(
        prediction: Mapping[str, Any], active: bool, market: str, mode: str, team: str | None,
        direction: str, line: float, odds: float, edge: float,
    ) -> None:  # fmt: skip
        result, actual = grade(market, line, team, direction, outcome)
        out.append(
            GradedLeg(
                sport=sport,
                slate=run["slate"],
                run_id=run["run_id"],
                game_id=row["game_id"],
                event_id=row["event_id"],
                market=market,
                same_level=active_same_level if active else not active_same_level,
                active=active,
                mode=mode,
                team=team,
                direction=direction,
                line=line,
                odds=odds,
                edge=edge,
                cross_level=_cross(prediction.get("flags") or ()),
                result=result,
                actual=actual,
            )  # fmt: skip
        )

    for key, active in (("prediction", True), ("other_variant", False)):
        prediction = row.get(key)
        if not prediction or not prediction.get("eligible"):
            continue
        edge = prediction.get("edge")
        if edge is not None and abs(edge) > EPS:
            direction = "Over" if edge > 0 else "Under"
            ladder = legs.get("TOTALS_ALL_OVER" if edge > 0 else "TOTALS_ALL_UNDER")
            if ladder:
                add(prediction, active, TOTALS, "main", None, direction, ladder["main_line"],
                    ladder["main_odds"], edge)  # fmt: skip
                add(prediction, active, TOTALS, "safest", None, direction, ladder["booked_line"],
                    ladder["odds"], edge)  # fmt: skip
        home, away = team_lines.get("home"), team_lines.get("away")
        if home or away:
            pick = team_pick(
                prediction["home_proj"], prediction["away_proj"],
                home["line"] if home else None, away["line"] if away else None, 0.0,
            )  # fmt: skip
            if pick is not None:
                chosen = home if pick.side == "home" else away
                assert chosen is not None  # noqa: S101 - team_pick only picks a side with a line.
                odds = chosen["over_odds"] if pick.direction == "Over" else chosen["under_odds"]
                add(prediction, active, TEAM_TOTAL, "main", pick.side, pick.direction,
                    pick.team_line, odds, pick.edge)  # fmt: skip
        elif active and legs.get("TEAM_TOTALS"):  # Runs before B6 kept only the picked side.
            leg = legs["TEAM_TOTALS"]
            add(prediction, True, TEAM_TOTAL, "main", leg["team"], leg["direction"],
                leg["main_line"], leg["main_odds"], leg["edge_main"])  # fmt: skip
    return out


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    document: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return document


def full_runs(slate_folder: Path) -> list[dict[str, Any]]:
    """Succeeded runs with rows, oldest first (run ids are ULIDs, so names sort by time)."""
    if not slate_folder.is_dir():
        return []
    runs = []
    for run_dir in sorted(p for p in slate_folder.iterdir() if p.is_dir()):
        document = _read(run_dir / "run.json")
        if (
            document
            and document.get("status") == "succeeded"
            and document.get("trigger") != REBOOK
            and "rows" in document
        ):
            runs.append(document)
    return runs


def last_rows_before_kickoff(
    runs: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Per game, the row from the newest run that started before its kickoff."""
    chosen: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for run in runs:
        started = datetime.fromisoformat(run["started_at"])
        for row in run["rows"]:
            if started < datetime.fromisoformat(row["kickoff"]):
                chosen[row["game_id"]] = (run, row)
    return sorted(chosen.values(), key=lambda pair: (pair[1]["kickoff"], pair[1]["game_id"]))


def ticket_results(
    slate_folder: Path, outcomes: Mapping[str, Outcome]
) -> dict[str, dict[str, Any]]:
    """Every ticket in the slate's index, graded on its booked lines."""
    index = _read(slate_folder / "index.json")
    if index is None:  # A run before B3 had no index: its tickets are in the newest full run.
        newest = full_runs(slate_folder)[-1:]
        index = {"tickets": {t["ticket_id"]: {"run_id": run["run_id"]}
                             for run in newest for t in run.get("tickets", [])}}  # fmt: skip
    runs: dict[str, dict[str, Any] | None] = {}
    out: dict[str, dict[str, Any]] = {}
    for ticket_id, entry in index["tickets"].items():
        run_id = entry["run_id"]
        if run_id not in runs:
            runs[run_id] = _read(slate_folder / run_id / "run.json")
        document = runs[run_id] or {}
        ticket = next((t for t in document.get("tickets", []) if t["ticket_id"] == ticket_id), None)
        if ticket is None:
            continue
        legs = []
        for leg in ticket.get("leg_details") or []:
            market = TOTALS if leg["team"] is None else TEAM_TOTAL
            outcome = outcomes.get(leg["game_id"], Outcome(PENDING))
            result, actual = grade(market, leg["booked_line"], leg["team"], leg["direction"],
                                   outcome)  # fmt: skip
            legs.append({"event_id": leg["event_id"], "result": result, "actual": actual})
        out[ticket_id] = {"result": ticket_result([leg["result"] for leg in legs]), "legs": legs}
    return out


def ticket_result(results: Sequence[str]) -> str | None:
    """Lost as soon as one leg loses; won when the rest are settled with at least one win."""
    if not results:
        return None
    if LOST in results:
        return LOST
    if PENDING in results:
        return PENDING
    return WON if WON in results else VOID


def grade_slate(
    data_dir: Path, sport: str, slate: date, outcomes: Mapping[str, Outcome], now: datetime
) -> dict[str, Any]:
    folder = runs_root(data_dir) / sport / slate.isoformat()
    legs = []
    for run, row in last_rows_before_kickoff(full_runs(folder)):
        legs += row_legs(run, row, outcomes.get(row["game_id"], Outcome(PENDING)))
    tickets = ticket_results(folder, outcomes)
    pending = sum(leg.result == PENDING for leg in legs) + sum(
        t["result"] == PENDING for t in tickets.values()
    )
    document = {
        "sport": sport,
        "slate": slate.isoformat(),
        "graded_at": now.isoformat(),
        "pending": pending,
        "legs": [asdict(leg) for leg in legs],
        "tickets": tickets,
    }
    path = grades_path(data_dir, sport, slate)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=1), "utf-8", newline="\n")
    os.replace(temporary, path)
    return document


def slates_to_grade(data_dir: Path, sport: str, today: date) -> list[date]:
    """Slates up to yesterday with runs, not yet graded or still pending, within REGRADE_DAYS."""
    folder = runs_root(data_dir) / sport
    if not folder.is_dir():
        return []
    out = []
    for slate_folder in sorted(p for p in folder.iterdir() if p.is_dir()):
        try:
            slate = date.fromisoformat(slate_folder.name)
        except ValueError:
            continue
        if slate >= today:
            continue
        existing = _read(grades_path(data_dir, sport, slate))
        if existing is None or (existing["pending"] and today - slate <= timedelta(REGRADE_DAYS)):
            out.append(slate)
    return out


def load_legs(
    data_dir: Path, sport: str, since: date | None = None, until: date | None = None
) -> list[GradedLeg]:
    folder = grades_root(data_dir) / sport
    out: list[GradedLeg] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        slate = date.fromisoformat(path.stem)
        if (since and slate < since) or (until and slate > until):
            continue
        document = _read(path) or {}
        out += [GradedLeg(**leg) for leg in document.get("legs", [])]
    return out


def load_tickets(
    data_dir: Path, sport: str, since: date | None = None, until: date | None = None
) -> list[dict[str, Any]]:
    folder = grades_root(data_dir) / sport
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        slate = date.fromisoformat(path.stem)
        if (since and slate < since) or (until and slate > until):
            continue
        for ticket_id, item in ((_read(path) or {}).get("tickets") or {}).items():
            out.append({"ticket_id": ticket_id, "slate": path.stem, **item})
    return out


# Calibration


@dataclass(frozen=True)
class Stats:
    legs: int  # Graded: won, lost or push.
    won: int
    lost: int
    push: int
    hit_rate: float | None  # Won over won + lost.
    implied: float | None  # Mean bookmaker-implied chance, 1/odds.
    above_implied: float | None
    roi: float | None  # Profit per unit staked.


def stats(legs: Iterable[GradedLeg]) -> Stats:
    settled = [leg for leg in legs if leg.result in (WON, LOST, PUSH)]
    won = sum(leg.result == WON for leg in settled)
    lost = sum(leg.result == LOST for leg in settled)
    decided = won + lost
    if not settled:
        return Stats(0, 0, 0, 0, None, None, None, None)
    implied = sum(1 / leg.odds for leg in settled) / len(settled)
    hit = won / decided if decided else None
    return Stats(
        legs=len(settled),
        won=won,
        lost=lost,
        push=len(settled) - decided,
        hit_rate=hit,
        implied=implied,
        above_implied=None if hit is None else hit - implied,
        roi=sum(leg.profit for leg in settled) / len(settled),
    )


def bucket_name(edge: float) -> str:
    size = abs(edge)
    for low, high in BUCKETS:
        if high is None or size < high:
            return f"{low:g}+" if high is None else f"{low:g}–{high:g}"
    raise AssertionError("unreachable")


def tables(legs: Sequence[GradedLeg]) -> list[dict[str, Any]]:
    """Brief §9's split: market × line mode × variant × edge bucket × cross-level."""
    groups: dict[tuple[Any, ...], list[GradedLeg]] = defaultdict(list)
    for leg in legs:
        key = (leg.market, leg.mode, leg.same_level, bucket_name(leg.edge), leg.cross_level)
        groups[key].append(leg)
    order = {name: i for i, name in enumerate(bucket_name(low) for low, _ in BUCKETS)}
    rows = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2], order[k[3]], k[4])):
        market, mode, same_level, bucket, cross = key
        labels = {"market": market, "mode": mode, "same_level": same_level, "bucket": bucket,
                  "cross_level": cross}  # fmt: skip
        rows.append({**labels, **asdict(stats(groups[key]))})
    return rows


def _qualifies(leg: GradedLeg, over: float, under: float) -> bool:
    if leg.direction == "Over":
        return leg.edge >= over - EPS
    return -leg.edge >= under - EPS


def _choice(
    legs: Sequence[GradedLeg], slates: int, keep: Callable[[GradedLeg], bool]
) -> dict[str, Any]:
    chosen = [leg for leg in legs if keep(leg)]
    numbers = asdict(stats(chosen))
    numbers["per_slate"] = numbers["legs"] / slates if slates else 0.0
    return numbers


def _keeper(thresholds: Mapping[str, float]) -> Callable[[GradedLeg], bool]:
    if "team_total_threshold" in thresholds:
        team = thresholds["team_total_threshold"]
        return lambda leg: abs(leg.edge) >= team - EPS
    over, under = thresholds["over_threshold"], thresholds["under_threshold"]
    return lambda leg: _qualifies(leg, over, under)


def _grid(
    market: str, sport_config: Mapping[str, Any]
) -> tuple[dict[str, float], list[dict[str, float]]]:
    """The thresholds in force, and every pair (totals) or value (team totals) to try."""
    if market == TOTALS:
        current = {"over_threshold": float(sport_config["over_threshold"]),
                   "under_threshold": float(sport_config["under_threshold"])}  # fmt: skip
        return current, [{"over_threshold": o, "under_threshold": u}
                         for o in THRESHOLDS for u in THRESHOLDS]  # fmt: skip
    current = {"team_total_threshold": float(sport_config["team_total_threshold"])}
    return current, [{"team_total_threshold": t} for t in THRESHOLDS]


def suggestions(
    legs: Sequence[GradedLeg], sport_config: Mapping[str, Any], min_legs: int
) -> list[dict[str, Any]]:
    """One per market × line mode, for the variant in force (D90). Picks the threshold(s) with
    the best flat-stake ROI among those keeping ≥ MIN_GRADED legs and ≥ `min_legs` per slate;
    a tie goes to the lower threshold, which keeps more legs. In-sample: the owner decides."""
    same_level = bool(sport_config["l5_same_level_only"])
    mine = [leg for leg in legs if leg.same_level == same_level]
    out = []
    for market in MARKETS:
        for mode in MODES[market]:
            pool = [leg for leg in mine if leg.market == market and leg.mode == mode]
            slates = len({leg.slate for leg in pool})
            current, grid = _grid(market, sport_config)
            best: tuple[float, int, dict[str, float], dict[str, Any]] | None = None
            for thresholds in grid:
                numbers = _choice(pool, slates, _keeper(thresholds))
                if numbers["legs"] < MIN_GRADED or numbers["per_slate"] < min_legs:
                    continue
                score = (numbers["roi"], numbers["legs"])
                if best is None or score > (best[0], best[1]):
                    best = (numbers["roi"], numbers["legs"], thresholds, numbers)
            out.append(
                {
                    "market": market,
                    "mode": mode,
                    "same_level": same_level,
                    "graded": stats(pool).legs,
                    "slates": slates,
                    "current": {
                        "thresholds": current,
                        "numbers": _choice(pool, slates, _keeper(current)),
                    },
                    "suggested": None
                    if best is None
                    else {"thresholds": best[2], "numbers": best[3]},
                }  # fmt: skip
            )
    return out


def apply_suggestion(
    document: Mapping[str, Any], sport: str, market: str, mode: str, thresholds: Mapping[str, float]
) -> dict[str, Any]:
    """The config document with one suggestion applied: its thresholds and its line mode."""
    copy: dict[str, Any] = json.loads(json.dumps(document))
    sport_config = copy["sports"][sport]
    sport_config.update(thresholds)
    sport_config["line_mode"]["totals" if market == TOTALS else "team_totals"] = mode
    return copy


def calibration(
    data_dir: Path, sport: str, document: Mapping[str, Any], since: date | None = None
) -> dict[str, Any]:
    """What the dashboard's Results page shows for one sport."""
    legs = load_legs(data_dir, sport, since)
    tickets = load_tickets(data_dir, sport, since)
    sport_config = document["sports"][sport]
    by_result: dict[str, int] = defaultdict(int)
    for ticket in tickets:
        by_result[ticket["result"] or "empty"] += 1
    return {
        "sport": sport,
        "graded_legs": sum(leg.result in (WON, LOST, PUSH) for leg in legs),
        "pending_legs": sum(leg.result == PENDING for leg in legs),
        "slates": len({leg.slate for leg in legs}),
        "min_graded": MIN_GRADED,
        "tickets": dict(by_result),
        "suggestions": suggestions(legs, sport_config, int(document["tickets"]["min_legs"])),
        "tables": tables(legs),
    }


# Weekly evidence report


def _percent(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.1f}%"


def _signed(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:+.1f}"


def _table_lines(rows: Iterable[Mapping[str, Any]], keys: Sequence[str]) -> list[str]:
    head = [*keys, "Legs", "Won", "Lost", "Push", "Hit", "Implied", "Hit − implied", "ROI"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        cells = [str(row[key]) for key in keys]
        cells += [str(row["legs"]), str(row["won"]), str(row["lost"]), str(row["push"]),
                  _percent(row["hit_rate"]), _percent(row["implied"]),
                  _signed(row["above_implied"]), _signed(row["roi"])]  # fmt: skip
        out.append("| " + " | ".join(cells) + " |")
    return out


def _summary_rows(legs: Sequence[GradedLeg]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, bool], list[GradedLeg]] = defaultdict(list)
    for leg in legs:
        groups[(leg.market, leg.mode, leg.same_level)].append(leg)
    return [
        {"Market": m, "Line": mode, "Same level only": "on" if s else "off", **asdict(stats(g))}
        for (m, mode, s), g in sorted(groups.items())
    ]


def report_text(
    sport: str, week_ending: date, document: Mapping[str, Any], data_dir: Path
) -> tuple[str, dict[str, Any]]:
    start = week_ending - timedelta(days=6)
    week = load_legs(data_dir, sport, start, week_ending)
    season = load_legs(data_dir, sport, None, week_ending)
    tickets = load_tickets(data_dir, sport, start, week_ending)
    result = calibration(data_dir, sport, document)
    lines = [
        f"# v2 evidence: {sport}, week ending {week_ending.isoformat()}",
        "",
        f"Generated by `hoopsedge.v2.grading report`. Week {start.isoformat()} to "
        f"{week_ending.isoformat()} (US Eastern slate dates). Brief §9, D90.",
        "",
        "Every game is graded at the last lines seen before kickoff, for both "
        "`l5_same_level_only` settings, whether or not it cleared a threshold. Hit rate counts "
        "wins over wins and losses; ROI is flat-stake, 1 unit per leg, pushes returned. "
        '"Implied" is the mean of 1/odds, margin included.',
        "",
        "## This week",
        "",
        *_table_lines(_summary_rows(week), ("Market", "Line", "Same level only")),
        "",
        f"Tickets: {len(tickets)} graded, "
        + ", ".join(
            f"{sum(t['result'] == name for t in tickets)} {name}"
            for name in (WON, LOST, PENDING, VOID)
        )
        + ".",
        "",
        "## Season to date",
        "",
        *_table_lines(_summary_rows(season), ("Market", "Line", "Same level only")),
        "",
        "## Threshold suggestions",
        "",
        f"For the setting in force. A suggestion needs {MIN_GRADED} graded legs clearing it, and "
        f"at least `min_legs` ({document['tickets']['min_legs']}) per slate on average. They are "
        "fitted on the same games they are scored on, so expect them to look better than they "
        "will do. Nothing changes until the owner applies one on the dashboard.",
        "",
    ]
    for item in result["suggestions"]:
        title = f"- **{item['market']}, {item['mode']} line:** "
        now = item["current"]
        current = (f"now {_thresholds(now['thresholds'])} gives {now['numbers']['legs']} legs, "
                   f"ROI {_signed(now['numbers']['roi'])}")  # fmt: skip
        if item["suggested"] is None:
            lines.append(f"{title}not enough yet ({item['graded']} graded). {current}.")
        else:
            best = item["suggested"]
            lines.append(
                f"{title}{_thresholds(best['thresholds'])} gives {best['numbers']['legs']} legs, "
                f"hit {_percent(best['numbers']['hit_rate'])} against "
                f"{_percent(best['numbers']['implied'])} implied, ROI "
                f"{_signed(best['numbers']['roi'])}. {current[0].upper()}{current[1:]}."
            )
    lines += [
        "",
        "## By edge bucket and cross-level, season to date",
        "",
        *_table_lines(
            [
                {
                    **row,
                    "Same level only": "on" if row["same_level"] else "off",
                    "Cross-level": "yes" if row["cross_level"] else "no",
                }
                for row in result["tables"]
            ],
            ("market", "mode", "Same level only", "bucket", "Cross-level"),
        ),  # fmt: skip
        "",
    ]
    return "\n".join(lines), {"week": [asdict(leg) for leg in week], "calibration": result}


def _thresholds(values: Mapping[str, float]) -> str:
    names = {"over_threshold": "over", "under_threshold": "under", "team_total_threshold": "team"}
    return " / ".join(f"{names[key]} {value:g}" for key, value in values.items())


def write_report(
    sport: str, week_ending: date, document: Mapping[str, Any], data_dir: Path, report_dir: Path
) -> Path:
    text, data = report_text(sport, week_ending, document, data_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v2-evidence-{sport}-{week_ending.isoformat()}"
    (report_dir / f"{stem}.md").write_text(text, "utf-8", newline="\n")
    (report_dir / f"{stem}.json").write_text(
        json.dumps(data, indent=1, sort_keys=True) + "\n", "utf-8", newline="\n"
    )
    return report_dir / f"{stem}.md"


# Command line


def _outcomes(data_dir: Path, sport: str, slates: Iterable[date]) -> dict[str, Outcome]:
    from hoopsedge.ingest.scores import SOURCES, read_results, season_for  # noqa: PLC0415

    seasons = {season_for(slate, SOURCES[sport]) for slate in slates}
    out: dict[str, Outcome] = {}
    for season in sorted(seasons):
        out |= outcomes_from(read_results(data_dir, sport, season).iter_rows(named=True))
    return out


def grade_all(
    data_dir: Path, sports: Iterable[str], today: date, now: datetime, only: date | None = None
) -> list[str]:
    lines = []
    for sport in sports:
        slates = [only] if only else slates_to_grade(data_dir, sport, today)
        if not slates:
            continue
        outcomes = _outcomes(data_dir, sport, slates)
        for slate in slates:
            document = grade_slate(data_dir, sport, slate, outcomes, now)
            settled = sum(leg["result"] in (WON, LOST, PUSH) for leg in document["legs"])
            lines.append(
                f"{sport} {slate}: {settled} legs graded, {document['pending']} pending, "
                f"{len(document['tickets'])} tickets"
            )
    return lines


def main() -> int:
    from hoopsedge.common.dynamo import connect, table_name  # noqa: PLC0415
    from hoopsedge.ingest.espn_files import today_eastern  # noqa: PLC0415
    from hoopsedge.v2.config_store import load as load_config  # noqa: PLC0415
    from hoopsedge.v2.views import SPORTS  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m hoopsedge.v2.grading")
    commands = parser.add_subparsers(dest="command", required=True)
    grade_command = commands.add_parser("grade", help="Grade finished slates")
    grade_command.add_argument("--sport", choices=SPORTS)
    grade_command.add_argument("--date", type=date.fromisoformat, help="Grade just this slate")
    report = commands.add_parser("report", help="Write the weekly evidence report")
    report.add_argument("--sport", choices=SPORTS)
    report.add_argument("--week-ending", type=date.fromisoformat)
    for command in (grade_command, report):
        command.add_argument("--data-dir", default=os.environ.get("HOOPSEDGE_DATA_DIR",
                                                                  DEFAULT_DATA_DIR))  # fmt: skip
    report.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    sports = [args.sport] if args.sport else list(SPORTS)
    today = today_eastern()
    if args.command == "grade":
        for line in grade_all(data_dir, sports, today, datetime.now(UTC), args.date):
            print(line)
        return 0
    _, config = load_config(connect().Table(table_name()))
    document = config.model_dump(by_alias=True, mode="json")
    week_ending = args.week_ending or today - timedelta(days=1)
    for sport in sports:
        print(write_report(sport, week_ending, document, data_dir, Path(args.report_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
