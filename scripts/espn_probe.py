"""Phase 0 ESPN probe (docs/01-phase0-feasibility.md sections 0.3 and 0.4).

Measures coverage on real responses and writes docs/reports/phase0-data-coverage.md.
Responses are cached under data/, so an interrupted run resumes cheaply.

    docker compose run --rm dev python ../scripts/espn_probe.py
    docker compose run --rm dev python ../scripts/espn_probe.py --record-fixtures
"""

import argparse
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.ingest.espn_coverage import (
    GameCheck,
    check_core_odds,
    check_scoreboard_event,
    check_summary,
)

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "docs" / "reports" / "phase0-data-coverage.md"
RESULTS = REPO / "docs" / "reports" / "phase0-data-coverage.json"
CACHE = REPO / "data" / "http-cache" / "espn"
FIXTURES = REPO / "services" / "tests" / "fixtures" / "espn"

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"
USER_AGENT = "HoopsEdge-probe/0.1 (private research client)"

LEAGUES: dict[str, dict[str, Any]] = {
    "NBA": {"path": "nba", "params": {}, "scan": ((10, 15), (6, 30))},
    "NCAA": {
        "path": "mens-college-basketball",
        "params": {"groups": "50", "limit": "400"},
        "scan": ((11, 1), (4, 15)),
    },
}
FIRST_SEASON = 2016  # 2015-16
CURRENT_SEASON = 2027  # 2026-27
SCAN_SEASONS = (2016, 2025)  # Oldest target season and latest complete season.
SCAN_SUMMARY_SAMPLE = 150
SCAN_ODDS_SAMPLE = 60
DEPTH_SAMPLE = 15
PRESEASON = 1
SEED = 20261105

# Keys dropped from fixtures: large, and unused by any check or parser.
FIXTURE_DROP = ("plays", "news", "article", "videos", "standings", "winprobability", "leaders")


def season_label(end_year: int) -> str:
    return f"{end_year - 1}-{str(end_year)[2:]}"


def scoreboard(client: PoliteClient, league: str, day: date) -> dict[str, Any]:
    config = LEAGUES[league]
    return client.get_json(
        f"{SITE}/{config['path']}/scoreboard",
        params={"dates": day.strftime("%Y%m%d"), **config["params"]},
    )


def summary(client: PoliteClient, league: str, game_id: str) -> dict[str, Any]:
    path = LEAGUES[league]["path"]
    return client.get_json(f"{SITE}/{path}/summary", params={"event": game_id})


def core_odds(client: PoliteClient, league: str, game_id: str) -> dict[str, Any]:
    """A 404 means ESPN holds no odds for this game. Other failures stop the probe."""
    path = LEAGUES[league]["path"]
    url = f"{CORE}/{path}/events/{game_id}/competitions/{game_id}/odds"
    try:
        return client.get_json(url, params={"limit": "50"})
    except HttpFetchError as error:
        if error.status == 404:
            return {"items": []}
        raise


def scan_dates(league: str, end_year: int) -> list[date]:
    (start_month, start_day), (end_month, end_day) = LEAGUES[league]["scan"]
    day = date(end_year - 1, start_month, start_day)
    last = date(end_year, end_month, end_day)
    return [day + timedelta(days=i) for i in range((last - day).days + 1)]


def depth_dates(end_year: int) -> list[date]:
    if end_year == CURRENT_SEASON:
        return [date(2026, 11, 3), date(2026, 11, 4)]
    return [date(end_year - 1, 12, 15), date(end_year, 1, 15), date(end_year, 2, 15)]


def collect_games(
    client: PoliteClient, league: str, days: list[date]
) -> tuple[list[GameCheck], Counter[int], int]:
    """Return non-preseason games, a count of all games by season type, and the busiest day."""
    games: list[GameCheck] = []
    by_type: Counter[int] = Counter()
    busiest = 0
    for day in days:
        events = scoreboard(client, league, day).get("events", [])
        busiest = max(busiest, len(events))
        for event in events:
            game = check_scoreboard_event(event, league)
            by_type[game.season_type or 0] += 1
            if game.season_type != PRESEASON:
                games.append(game)
    return games, by_type, busiest


def percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def scoreboard_metrics(games: list[GameCheck], days: list[date]) -> dict[str, Any]:
    exhibitions = sum(g.is_exhibition for g in games)
    games = [g for g in games if not g.is_exhibition]
    finals = [g for g in games if g.is_final]
    requested = set(days)
    return {
        "exhibitions_excluded": exhibitions,
        "games_seen": len(games),
        "final_games": len(finals),
        "pct_periods_present": percent(sum(g.periods_present for g in finals), len(finals)),
        "pct_half_available": percent(sum(g.half_available for g in finals), len(finals)),
        "pct_periods_match_score": percent(sum(g.periods_match_score for g in finals), len(finals)),
        "pct_neutral_known": percent(sum(g.neutral_known for g in finals), len(finals)),
        "pct_venue_known": percent(sum(g.venue_known for g in finals), len(finals)),
        "pct_on_requested_et_date": percent(sum(g.et_date in requested for g in games), len(games)),
        "overtime_games": sum(g.ot_periods > 0 for g in finals),
    }


def summary_metrics(client: PoliteClient, league: str, game_ids: list[str]) -> dict[str, Any]:
    missing: Counter[str] = Counter()
    relations: Counter[str] = Counter()
    pickcenter: Counter[str] = Counter()
    complete = minutes = starters = 0
    plausible = plausible_known = injury_rows = injury_after = games_with_injuries = 0
    for game_id in game_ids:
        check = check_summary(summary(client, league, game_id), league)
        missing.update(check.missing_fields)
        relations.update(check.turnover_relations)
        pickcenter.update(check.pickcenter_providers)
        complete += not check.missing_fields
        minutes += check.player_minutes_present
        starters += check.starter_flag_present
        if check.possessions_plausible is not None:
            plausible_known += 1
            plausible += check.possessions_plausible
        injury_rows += check.injury_rows
        injury_after += check.injury_rows_after_tip
        games_with_injuries += check.injury_rows > 0
    return {
        "sampled": len(game_ids),
        "pct_box_complete": percent(complete, len(game_ids)),
        "missing_fields": dict(missing),
        "pct_player_minutes": percent(minutes, len(game_ids)),
        "pct_starter_flag": percent(starters, len(game_ids)),
        "pct_possessions_plausible": percent(plausible, plausible_known),
        "turnover_relations": dict(relations),
        "games_with_injury_rows": games_with_injuries,
        "injury_rows": injury_rows,
        "injury_rows_after_tip": injury_after,
        "pickcenter_providers": dict(pickcenter),
    }


def odds_metrics(client: PoliteClient, league: str, game_ids: list[str]) -> dict[str, Any]:
    providers: Counter[str] = Counter()
    books: Counter[str] = Counter()
    signs: Counter[str] = Counter()
    with_line = with_close = 0
    for game_id in game_ids:
        check = check_core_odds(core_odds(client, league, game_id))
        providers.update(check.all_providers)
        books.update(check.book_providers)
        with_line += check.has_book_line
        with_close += check.has_open_close
        if check.spread_sign:
            signs[check.spread_sign] += 1
    return {
        "sampled": len(game_ids),
        "pct_with_book_line": percent(with_line, len(game_ids)),
        "pct_with_open_close": percent(with_close, len(game_ids)),
        "providers": dict(providers.most_common()),
        "book_providers": dict(books.most_common()),
        "spread_sign": dict(signs),
    }


def sample_ids(games: list[GameCheck], size: int, rng: random.Random) -> list[str]:
    """Draw from all finals, then drop exhibitions, so samples match the cached responses."""
    ids = sorted(g.game_id for g in games if g.is_final)
    exhibitions = {g.game_id for g in games if g.is_exhibition}
    return [
        game_id for game_id in rng.sample(ids, min(size, len(ids))) if game_id not in exhibitions
    ]


def injuries_snapshot(client: PoliteClient) -> dict[str, Any]:
    document = client.get_json(f"{SITE}/nba/injuries")
    rows = [row for team in document.get("injuries", []) for row in team.get("injuries", [])]
    return {
        "teams": len(document.get("injuries", [])),
        "rows": len(rows),
        "statuses": dict(Counter(str(row.get("status")) for row in rows)),
        "type_names": dict(Counter(str(row.get("type", {}).get("name")) for row in rows)),
        "fantasy_statuses": dict(
            Counter(
                str(row.get("details", {}).get("fantasyStatus", {}).get("abbreviation"))
                for row in rows
            )
        ),
        "rows_with_athlete_id": sum(
            "/id/" in json.dumps(row.get("athlete", {}).get("links", [])) for row in rows
        ),
    }


def run(client: PoliteClient, live_client: PoliteClient) -> dict[str, Any]:
    rng = random.Random(SEED)  # noqa: S311 - reproducible sampling, not security.
    results: dict[str, Any] = {"generated_at": datetime.now().astimezone().isoformat()}
    for league in LEAGUES:
        league_results: dict[str, Any] = {"scans": {}, "depth": {}}
        for end_year in SCAN_SEASONS:
            label = season_label(end_year)
            print(f"[{league}] full scan {label}", flush=True)
            days = scan_dates(league, end_year)
            games, by_type, busiest = collect_games(client, league, days)
            league_results["scans"][label] = {
                **scoreboard_metrics(games, days),
                "games_by_season_type": dict(by_type),
                "busiest_day_games": busiest,
                "summaries": summary_metrics(
                    client, league, sample_ids(games, SCAN_SUMMARY_SAMPLE, rng)
                ),
                "odds": odds_metrics(client, league, sample_ids(games, SCAN_ODDS_SAMPLE, rng)),
            }
        for end_year in range(FIRST_SEASON, CURRENT_SEASON + 1):
            label = season_label(end_year)
            print(f"[{league}] depth {label}", flush=True)
            days = depth_dates(end_year)
            games, _, _ = collect_games(client, league, days)
            league_results["depth"][label] = {
                **scoreboard_metrics(games, days),
                "summaries": summary_metrics(client, league, sample_ids(games, DEPTH_SAMPLE, rng)),
                "odds": odds_metrics(client, league, sample_ids(games, DEPTH_SAMPLE, rng)),
            }
        results[league] = league_results
    results["nba_injuries"] = injuries_snapshot(live_client)
    latencies = sorted(client.stats.latencies)
    results["client"] = {
        "network_requests": client.stats.requests,
        "cache_hits": client.stats.cache_hits,
        "retries": client.stats.retries,
        "by_status": {str(k): v for k, v in client.stats.by_status.items()},
        "median_latency_seconds": round(latencies[len(latencies) // 2], 3) if latencies else None,
    }
    return results


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join("n/a" if c is None else str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def top(counts: dict[str, int], limit: int = 4) -> str:
    items = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{name} ({count})" for name, count in items) or "none"


def write_report(results: dict[str, Any]) -> str:
    parts = [
        "# Phase 0: ESPN Data Coverage",
        "",
        f"Generated by `scripts/espn_probe.py` on {results['generated_at'][:10]}. "
        "Raw numbers are in `phase0-data-coverage.json`.",
    ]
    for league in LEAGUES:
        data = results[league]
        parts += ["", f"## {league}", "", "### Full-season scans", ""]
        parts.append(
            table(
                [
                    "Season",
                    "Final games",
                    "Exhibitions excluded",
                    "Busiest day",
                    "Half scores %",
                    "Periods = score %",
                    "Neutral known %",
                    "Venue known %",
                    "On requested ET date %",
                ],
                [
                    [
                        label,
                        s["final_games"],
                        s["exhibitions_excluded"],
                        s["busiest_day_games"],
                        s["pct_half_available"],
                        s["pct_periods_match_score"],
                        s["pct_neutral_known"],
                        s["pct_venue_known"],
                        s["pct_on_requested_et_date"],
                    ]
                    for label, s in data["scans"].items()
                ],
            )
        )
        parts += ["", "### Box scores (sampled summaries)", ""]
        sections = list(data["scans"].items()) + list(data["depth"].items())
        parts.append(
            table(
                [
                    "Season",
                    "Sample",
                    "Box complete %",
                    "Minutes %",
                    "Starter flag %",
                    "Possessions plausible %",
                    "Injury rows dated after tip",
                ],
                [
                    [
                        label,
                        s["summaries"]["sampled"],
                        s["summaries"]["pct_box_complete"],
                        s["summaries"]["pct_player_minutes"],
                        s["summaries"]["pct_starter_flag"],
                        s["summaries"]["pct_possessions_plausible"],
                        f"{s['summaries']['injury_rows_after_tip']} of "
                        f"{s['summaries']['injury_rows']}",
                    ]
                    for label, s in sections
                ],
            )
        )
        parts += ["", "### Historical lines (core odds API, sampled)", ""]
        parts.append(
            table(
                [
                    "Season",
                    "Sample",
                    "Sportsbook line %",
                    "Open and close %",
                    "Top books",
                    "Spread sign",
                ],
                [
                    [
                        label,
                        s["odds"]["sampled"],
                        s["odds"]["pct_with_book_line"],
                        s["odds"]["pct_with_open_close"],
                        top(s["odds"]["book_providers"], 3),
                        top(s["odds"]["spread_sign"], 2),
                    ]
                    for label, s in sections
                ],
            )
        )
        relations: Counter[str] = Counter()
        for _, section in sections:
            relations.update(section["summaries"]["turnover_relations"])
        parts += ["", f"**Turnover field relationship:** {top(dict(relations), 5)}"]
    injuries = results["nba_injuries"]
    client = results["client"]
    parts += [
        "",
        "## Limits of this report",
        "",
        "- Odds items hold full-game spreads and totals only. No first-half or team-total lines "
        "were found, and the `propBets` feed sampled for one game holds player props only.",
        "- Season rows other than the full scans rest on 13-15 games from 2-3 dates each, so "
        "treat them as indications, not measurements.",
        "- Possessions use player turnovers plus team turnovers, not the reported team total.",
        "",
        "## NBA injuries endpoint",
        "",
        f"- Teams listed: {injuries['teams']}; rows: {injuries['rows']}",
        f"- Statuses: {top(injuries['statuses'], 8)}",
        f"- Type names: {top(injuries['type_names'], 8)}",
        f"- Fantasy statuses: {top(injuries['fantasy_statuses'], 8)}",
        "",
        "## Rate behaviour",
        "",
        "These counts cover the latest run only, and cached responses make no requests. "
        "`docs/DECISIONS.md` (D28) has the figures from the run that fetched the data.",
        "",
        f"- Network requests this run: {client['network_requests']}; "
        f"cache hits: {client['cache_hits']}",
        f"- Retries: {client['retries']}; status counts: {client['by_status']}",
        f"- Median latency: {client['median_latency_seconds']} s",
        "",
    ]
    return "\n".join(parts)


def scrub_fixture(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in FIXTURE_DROP}


def record_fixtures(client: PoliteClient, live_client: PoliteClient) -> None:
    """Save three trimmed real examples of each response type for later parser tests."""
    examples = {
        "NBA": [date(2016, 12, 14), date(2025, 1, 15), date(2026, 11, 4)],
        "NCAA": [date(2017, 1, 14), date(2025, 1, 18), date(2026, 11, 4)],
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for league, days in examples.items():
        for day in days:
            board = scoreboard(client, league, day)
            finals = [
                e for e in board.get("events", []) if check_scoreboard_event(e, league).is_final
            ]
            if not finals:
                raise RuntimeError(f"No final games to record for {league} on {day}")
            game_id = str(finals[0]["id"])
            stem = f"{league.lower()}-{day:%Y%m%d}"
            trimmed_board = {**board, "events": finals[:3]}
            _write_json(FIXTURES / f"{stem}-scoreboard.json", trimmed_board)
            _write_json(
                FIXTURES / f"{stem}-summary.json", scrub_fixture(summary(client, league, game_id))
            )
            _write_json(FIXTURES / f"{stem}-odds.json", core_odds(client, league, game_id))
    injuries = live_client.get_json(f"{SITE}/nba/injuries")
    injuries["injuries"] = injuries.get("injuries", [])[:3]
    _write_json(FIXTURES / "nba-injuries.json", injuries)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-fixtures", action="store_true")
    args = parser.parse_args()

    client = PoliteClient(USER_AGENT, rate_per_second=2.0, cache_dir=CACHE)
    live_client = PoliteClient(USER_AGENT, rate_per_second=2.0)
    try:
        if args.record_fixtures:
            record_fixtures(client, live_client)
            print(f"Fixtures written to {FIXTURES}")
            return
        results = run(client, live_client)
    finally:
        client.close()
        live_client.close()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    _write_json(RESULTS, results)
    REPORT.write_text(write_report(results), encoding="utf-8")
    print(f"Report written to {REPORT}")


if __name__ == "__main__":
    main()
