"""Read-only views of v2 runs for the API and dashboard (B4, D98). Files only, never a write.

Runs live in `data/v2/runs/{sport}/{date}/{run_id}/run.json`, with the slate's `index.json`
saying which tickets were superseded or rebooked. Run IDs are ULIDs, so their names sort by time.
A slate's rows come from its newest full run (a rebook run has tickets but no rows).
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

SPORTS = ("NBA", "NCAAB", "NCAAF")
REBOOK = "rebook"


def runs_root(data_dir: Path) -> Path:
    return data_dir / "v2" / "runs"


def slate_dir(data_dir: Path, sport: str, day: date) -> Path:
    return runs_root(data_dir) / sport / day.isoformat()


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    document: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return document


def _run_dirs(folder: Path) -> list[Path]:
    """Newest first."""
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)


def latest_full_run(folder: Path) -> tuple[Path, dict[str, Any]] | None:
    for run_dir in _run_dirs(folder):
        document = _read(run_dir / "run.json")
        if document and document.get("trigger") != REBOOK and "rows" in document:
            return run_dir, document
    return None


def run_summary(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": document["run_id"],
        "sport": document["sport"],
        "slate": document["slate"],
        "trigger": document.get("trigger", "manual"),
        "status": document.get("status", "unknown"),
        "dry_run": bool(document.get("dry_run", True)),
        "error": document.get("error"),
        "started_at": document.get("started_at"),
        "ended_at": document.get("ended_at"),
        "config_version": document.get("config_version"),
        "counts": document.get("counts") or {},
        "tickets": len(document.get("tickets") or []),
    }


def _started(kickoff: str, now: datetime) -> bool:
    return datetime.fromisoformat(kickoff) <= now


def row_view(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    p = row["prediction"]
    other = row.get("other_variant") or {}
    legs = row.get("legs") or {}
    lean = legs.get("TOTALS_LEAN")
    team = legs.get("TEAM_TOTALS")
    pick = p.get("team_pick")
    team_pick = None
    if pick:
        side_name = row["home"] if pick["side"] == "home" else row["away"]
        team_pick = {
            "team": side_name,
            "side": pick["side"],
            "direction": pick["direction"],
            "line": team["booked_line"] if team else pick["team_line"],
            "odds": team["odds"] if team else None,
            "projection": pick["proj"],
            "edge": pick["edge"],
        }

    def l5(side: dict[str, Any]) -> dict[str, Any]:
        return {"pf": side["pf"], "pa": side["pa"], "n": side["n"]}

    return {
        "event_id": row["event_id"],
        "game_id": row["game_id"],
        "home": row["home"],
        "away": row["away"],
        "kickoff": row["kickoff"],
        "started": _started(row["kickoff"], now),
        "eligible": p["eligible"],
        "skip_reason": p.get("skip_reason"),
        "home_tiers": list(p["home_tiers"]),
        "away_tiers": list(p["away_tiers"]),
        "home_l5": l5(p["home"]),
        "away_l5": l5(p["away"]),
        "sum_of_avgs": p["sum_of_avgs"],
        "model_total": p["model_total"],
        "outlook": p["outlook"],
        "main_line": p.get("main_line"),
        "lean": p.get("lean"),
        "edge_main": p.get("edge"),
        "booked_line": lean["booked_line"] if lean else None,
        "booked_odds": lean["odds"] if lean else None,
        "edge_booked": lean["edge_booked"] if lean else None,
        "home_proj": p["home_proj"],
        "away_proj": p["away_proj"],
        "team_pick": team_pick,
        "flags": list(p.get("flags") or []),
        "same_level_total": other.get("model_total"),
        "same_level_lean": other.get("lean"),
    }


def leg_view(leg: dict[str, Any], now: datetime) -> dict[str, Any]:
    keys = (
        "kind", "event_id", "home", "away", "kickoff", "mode", "team", "direction",
        "projection", "booked_line", "main_line", "odds", "main_odds", "edge_main",
        "edge_booked", "flags",
    )  # fmt: skip
    view = {key: leg.get(key) for key in keys}
    view["flags"] = list(leg.get("flags") or [])
    view["started"] = _started(leg["kickoff"], now)
    return view


def ticket_view(
    ticket: dict[str, Any],
    entry: dict[str, Any] | None,
    run: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """A ticket with what has happened to it since (from the index) and its legs' state now."""
    entry = entry or {}
    legs = [leg_view(leg, now) for leg in ticket.get("leg_details") or []]
    upcoming = sorted(leg["kickoff"] for leg in legs if not leg["started"])
    status = entry.get("status", ticket["status"])
    return {
        "ticket_id": ticket["ticket_id"],
        "sport": run["sport"],
        "slate": run["slate"],
        "run_id": run["run_id"],
        "kind": ticket["kind"],
        "number": ticket["number"],
        "status": status,
        "code": ticket.get("code"),
        "reason": ticket.get("reason"),
        "dry_run": bool(ticket.get("dry_run", True)),
        "total_odds": ticket["total_odds"],
        "bookmaker_implied_probability": ticket["bookmaker_implied_probability"],
        "earliest_kickoff": ticket["earliest_kickoff"],
        "next_kickoff": upcoming[0] if upcoming else None,
        "started_legs": sum(leg["started"] for leg in legs),
        "model_disagrees": ticket.get("model_disagrees", 0),
        "superseded_by": entry.get("superseded_by"),
        "rebooked_by": entry.get("rebooked_by"),
        "rebook_of": ticket.get("rebook_of") or entry.get("rebook_of"),
        "current": status != "superseded" and not entry.get("rebooked_by"),
        "legs": legs,
    }


def _tickets(folder: Path, latest: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    index = _read(folder / "index.json")
    runs: dict[str, dict[str, Any] | None] = {}

    def run(run_id: str) -> dict[str, Any] | None:
        if run_id not in runs:
            runs[run_id] = _read(folder / run_id / "run.json")
        return runs[run_id]

    views: list[dict[str, Any]] = []
    if index:
        for ticket_id, entry in index["tickets"].items():
            document = run(entry["run_id"])
            found = next(
                (t for t in (document or {}).get("tickets", []) if t["ticket_id"] == ticket_id),
                None,
            )
            if document and found:
                views.append(ticket_view(found, entry, document, now))
    elif latest:  # A slate run before B3 had no index.
        views = [ticket_view(t, None, latest, now) for t in latest.get("tickets", [])]
    views.sort(key=lambda t: (not t["current"], t["kind"], t["number"], t["run_id"]))
    return views


def slate_view(data_dir: Path, sport: str, day: date, now: datetime) -> dict[str, Any]:
    folder = slate_dir(data_dir, sport, day)
    found = latest_full_run(folder)
    latest = found[1] if found else None
    rows = [row_view(row, now) for row in (latest or {}).get("rows", [])]
    return {
        "sport": sport,
        "run": run_summary(latest) if latest else None,
        "rows": rows,
        "tickets": _tickets(folder, latest, now),
        "unmatched": len((latest or {}).get("unmatched", [])),
        "csv": bool(found and any(found[0].glob("*.csv"))),
    }


def csv_file(data_dir: Path, sport: str, day: date) -> Path | None:
    found = latest_full_run(slate_dir(data_dir, sport, day))
    if not found:
        return None
    files = sorted(found[0].glob("*.csv"))
    return files[0] if files else None


def find_ticket(data_dir: Path, ticket_id: str, now: datetime) -> dict[str, Any] | None:
    for index_file in sorted(runs_root(data_dir).glob("*/*/index.json")):
        entry = (_read(index_file) or {}).get("tickets", {}).get(ticket_id)
        if entry is None:
            continue
        document = _read(index_file.parent / entry["run_id"] / "run.json")
        for ticket in (document or {}).get("tickets", []):
            if ticket["ticket_id"] == ticket_id and document:
                return ticket_view(ticket, entry, document, now)
    return None


def _all_run_dirs(data_dir: Path, sport: str | None) -> list[Path]:
    pattern = f"{sport}/*/*" if sport else "*/*/*"
    dirs = [p for p in runs_root(data_dir).glob(pattern) if p.is_dir() and p.name != "raw"]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def list_runs(data_dir: Path, limit: int, sport: str | None = None) -> list[dict[str, Any]]:
    out = []
    for run_dir in _all_run_dirs(data_dir, sport):
        document = _read(run_dir / "run.json")
        if document:
            out.append(run_summary(document))
        if len(out) >= limit:
            break
    return out


def run_detail(data_dir: Path, run_id: str, now: datetime) -> dict[str, Any] | None:
    for run_dir in runs_root(data_dir).glob(f"*/*/{run_id}"):
        document = _read(run_dir / "run.json")
        if not document:
            continue
        index = (_read(run_dir.parent / "index.json") or {}).get("tickets", {})
        reasons: dict[str, int] = {}
        for item in document.get("dropped", []):
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        return {
            **run_summary(document),
            "tickets_detail": [
                ticket_view(t, index.get(t["ticket_id"]), document, now)
                for t in document.get("tickets", [])
            ],
            "unmatched_events": document.get("unmatched", []),
            "failures": document.get("failures", []),
            "dropped": reasons,
        }
    return None


def last_run_per_sport(data_dir: Path) -> dict[str, dict[str, Any] | None]:
    return {sport: next(iter(list_runs(data_dir, 1, sport)), None) for sport in SPORTS}
