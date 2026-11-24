"""The v2 API (D98): the secret, slates, tickets, runs, CSV, review queue, aliases, config, system.

Run folders are made by the real code path (slate build, tickets, dry booking, the index), so these
tests read exactly what a run writes.
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from hoopsedge.api.app import Deps, changes_between, create_app, openapi_document
from hoopsedge.api.main import OPENAPI_FILE, LocalRunStarter, export_openapi
from hoopsedge.api.store import ApiStore, Artifacts
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.v2.booking import BOOKED, Booker, book_ticket, record_run
from hoopsedge.v2.config_store import (
    SCHEDULE_NAMES,
    default_document,
    document_from_stored,
    load,
)
from hoopsedge.v2.grading import FINAL, Outcome, grade_slate
from hoopsedge.v2.slate import _row_document, csv_rows, csv_text, ticket_document
from hoopsedge.v2.tickets import build_tickets
from tests.test_v2_grading import season, write_grades
from tests.test_v2_slate import CONFIG, KICKOFF, ladder_event, slate_build

KEY = "test-secret-value"
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
SLATE = date(2026, 11, 28)
ZONE = ZoneInfo("Africa/Lagos")
TEAMS = {
    "NCAAF": [
        {"team_id": "48", "name": "Delaware Blue Hens", "short_name": "Delaware", "level": "FBS"},
        {"team_id": "41", "name": "UConn Huskies", "short_name": "UConn", "level": "FBS"},
    ]
}


class FakeStarter:
    def __init__(self) -> None:
        self.started: list[tuple[str, date]] = []
        self.rebooked: list[tuple[str, date, str]] = []

    def start(self, sport: str, slate: date) -> None:
        self.started.append((sport, slate))

    def rebook(self, sport: str, slate: date, ticket_id: str) -> None:
        self.rebooked.append((sport, slate, ticket_id))


class FakeSchedules:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str]] = []

    def update(self, name: str, time: str) -> None:
        self.updates.append((name, time))


class Api:
    def __init__(self, table: Any, root: Path) -> None:
        self.table = table
        self.root = root
        self.store = ApiStore(table)
        self.starter = FakeStarter()
        self.schedules = FakeSchedules()
        self.relay = {"mode": "proxy", "reachable": True, "checked_at": NOW.isoformat()}
        self.secret: str | None = KEY
        self.deps = Deps(
            self.store,
            Artifacts(root),
            self.starter,
            self.schedules,
            secret=lambda: self.secret,
            now=lambda: NOW,
            relay=lambda: self.relay,
            teams=lambda sport: TEAMS.get(sport, []),
        )
        self.client = TestClient(create_app(self.deps), raise_server_exceptions=True)
        self._keys = 0

    def get(self, path: str, **params: Any) -> Any:
        return self.client.get(path, headers={"x-hoopsedge-key": KEY}, params=params)

    def send(self, method: str, path: str, body: Any = None) -> Any:
        self._keys += 1
        headers = {"x-hoopsedge-key": KEY, "idempotency-key": f"key-{self._keys}"}
        return self.client.request(method, path, headers=headers, json=body)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Api]:
    monkeypatch.setenv("HOOPSEDGE_ENV", "local")
    with mock_aws():
        resource = boto3.resource(
            "dynamodb", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="x",  # noqa: S106 - Dummy values.
        )  # fmt: skip
        ensure_table(resource, "t")
        yield Api(resource.Table("t"), tmp_path)


def write_run(
    root: Path, run_id: str, status: str = "succeeded", booked: bool = False
) -> dict[str, Any]:
    """One NCAAF run on SLATE: Delaware hosts UConn twice (two events), one 2-leg lean ticket."""
    events = [
        ladder_event([47.5, 48.5, 49.5], main=48.5, team_totals=(23.5, 24.5)),
        ladder_event([47.5, 48.5, 49.5], main=48.5, event_id="sr:match:2",
                     start=KICKOFF + timedelta(minutes=30), team_totals=(23.5, 24.5)),
    ]  # fmt: skip
    build = slate_build(events)
    folder = root / "v2" / "runs" / "NCAAF" / SLATE.isoformat() / run_id
    folder.mkdir(parents=True)
    legs = [row.legs["TOTALS_LEAN"] for row in build.rows]
    [ticket] = build_tickets("TOTALS_LEAN", legs, CONFIG.tickets, NOW)[0]
    result = book_ticket(Booker(None, False, lambda *_: None), ticket, f"T{run_id}",
                         lambda given: (list(given), []), 2)  # fmt: skip
    if booked:
        result = replace(result, status=BOOKED, code=f"REAL{run_id[-2:]}")
    tickets = [ticket_document(ticket, f"T{run_id}", result, ZONE)]
    document = {
        "run_id": run_id, "sport": "NCAAF", "slate": SLATE.isoformat(), "trigger": "scheduled",
        "status": status, "dry_run": not booked, "started_at": NOW.isoformat(),
        "config": CONFIG.model_dump(by_alias=True, mode="json"),
        "config_version": 0, "counts": {"games": len(build.rows)},
        "rows": [_row_document(row) for row in build.rows], "tickets": tickets,
        "dropped": [{"kind": "TEAM_TOTALS", "reason": "inside_cutoff", "leg": {}}],
        "unmatched": [], "failures": [],
    }  # fmt: skip
    (folder / "run.json").write_text(json.dumps(document, default=str), "utf-8")
    (folder / "ncaaf_predictions_20261128.csv").write_text(csv_text(csv_rows(build, ZONE)), "utf-8")
    record_run(folder.parent, run_id, "scheduled", tickets)
    return document


ALL_GETS = [
    "/health/details", f"/slates/{SLATE}", "/runs", "/unmatched", "/config", "/config/versions",
    "/system", "/teams/NCAAF", "/aliases/NCAAF", "/calibration/NCAAB",
]  # fmt: skip
ALL_MUTATIONS = [
    ("POST", "/runs"), ("PUT", "/config"), ("PUT", "/schedules/nba_slate"),
    ("POST", "/tickets/x/rebook"), ("POST", "/aliases"), ("DELETE", "/aliases/NCAAF/x"),
    ("POST", "/unmatched/x/resolve"), ("POST", "/calibration/NCAAB/apply"),
]  # fmt: skip


# The secret (D8).


@pytest.mark.parametrize("path", ALL_GETS)
def test_every_read_is_refused_without_the_secret(api: Api, path: str) -> None:
    assert api.client.get(path).status_code == 401
    assert api.client.get(path, headers={"x-hoopsedge-key": "wrong"}).status_code == 401


@pytest.mark.parametrize(("method", "path"), ALL_MUTATIONS)
def test_every_mutation_is_refused_without_the_secret(api: Api, method: str, path: str) -> None:
    response = api.client.request(method, path, json={}, headers={"idempotency-key": "k"})
    assert response.status_code == 401


def test_no_secret_configured_lets_nobody_in(api: Api) -> None:
    api.secret = None
    assert api.client.get("/config", headers={"x-hoopsedge-key": ""}).status_code == 401


def test_health_needs_no_secret_and_says_nothing_else(api: Api) -> None:
    assert api.client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("path", ALL_GETS)
def test_every_read_works_on_an_empty_system(api: Api, path: str) -> None:
    assert api.get(path).status_code == 200, path


def test_mutations_need_an_idempotency_key(api: Api) -> None:
    response = api.client.post("/runs", json={"sport": "NBA"}, headers={"x-hoopsedge-key": KEY})
    assert response.status_code == 400


def test_v1_routes_are_gone(api: Api) -> None:
    for path in ("/models/NBA", "/evidence/NBA", "/track-record", "/games/x"):
        assert api.get(path).status_code == 404, path


# Slates.


def test_a_slate_shows_rows_and_tickets_from_the_newest_full_run(api: Api) -> None:
    write_run(api.root, "01AAAA")
    body = api.get(f"/slates/{SLATE}", sport="NCAAF").json()
    [sport] = body["sports"]
    assert sport["run"]["run_id"] == "01AAAA" and sport["csv"] is True
    first = sport["rows"][0]
    assert (first["home"], first["away"], first["lean"]) == ("Delaware", "UConn", "OVER")
    assert first["model_total"] == 53.0 and first["booked_line"] == 47.5
    assert first["team_pick"]["team"] == "Delaware" and first["team_pick"]["odds"] == 1.87
    assert first["started"] is False
    [ticket] = sport["tickets"]
    assert ticket["status"] == "dry_run" and ticket["current"] is True
    assert ticket["legs"][0]["direction"] == "Over" and len(ticket["legs"]) == 2
    assert "Bookmaker-implied" in ticket["implied_note"]


def test_every_sport_is_listed_even_without_runs(api: Api) -> None:
    sports = api.get(f"/slates/{SLATE}").json()["sports"]
    assert [s["sport"] for s in sports] == ["NBA", "NCAAB", "NCAAF"]
    assert all(s["run"] is None and s["rows"] == [] for s in sports)


def test_later_real_codes_supersede_earlier_ones(api: Api) -> None:
    write_run(api.root, "01AAAA", booked=True)
    write_run(api.root, "01BBBB", booked=True)
    tickets = api.get(f"/slates/{SLATE}", sport="NCAAF").json()["sports"][0]["tickets"]
    by_id = {t["ticket_id"]: t for t in tickets}
    assert by_id["T01BBBB"]["current"] and by_id["T01BBBB"]["status"] == "booked"
    assert by_id["T01AAAA"]["status"] == "superseded" and not by_id["T01AAAA"]["current"]
    assert by_id["T01AAAA"]["superseded_by"] == "01BBBB"
    assert tickets[0]["ticket_id"] == "T01BBBB"  # Current tickets first.


def test_the_csv_is_the_owner_format(api: Api) -> None:
    write_run(api.root, "01AAAA")
    response = api.get(f"/slates/{SLATE}/NCAAF/csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "ncaaf_predictions_20261128.csv" in response.headers["content-disposition"]
    assert response.text.splitlines()[0].startswith("Away,Home,Away Atk/Def")
    assert api.get(f"/slates/{SLATE}/NBA/csv").status_code == 404


def test_a_bad_date_is_refused(api: Api) -> None:
    assert api.get("/slates/yesterday").status_code == 422


# Tickets and rebook.


def test_a_ticket_is_found_by_id(api: Api) -> None:
    write_run(api.root, "01AAAA")
    body = api.get("/tickets/T01AAAA").json()
    assert body["sport"] == "NCAAF" and body["slate"] == SLATE.isoformat()
    assert body["next_kickoff"] == KICKOFF.isoformat() and body["started_legs"] == 0
    assert api.get("/tickets/nope").status_code == 404
    assert api.get("/tickets/bad id!").status_code == 422


def test_rebook_starts_a_rebook_of_that_ticket(api: Api) -> None:
    write_run(api.root, "01AAAA")
    response = api.send("POST", "/tickets/T01AAAA/rebook")
    assert response.status_code == 202
    assert api.starter.rebooked == [("NCAAF", SLATE, "T01AAAA")]
    assert api.send("POST", "/tickets/nope/rebook").status_code == 404


# Runs and Run now.


def test_runs_are_newest_first_and_filter_by_sport(api: Api) -> None:
    write_run(api.root, "01AAAA")
    write_run(api.root, "01BBBB")
    assert [r["run_id"] for r in api.get("/runs").json()] == ["01BBBB", "01AAAA"]
    assert api.get("/runs", sport="NBA").json() == []
    assert api.get("/runs", limit=1).json()[0]["run_id"] == "01BBBB"


def test_a_run_shows_its_tickets_and_drops(api: Api) -> None:
    write_run(api.root, "01AAAA")
    body = api.get("/runs/01AAAA").json()
    assert body["tickets_detail"][0]["ticket_id"] == "T01AAAA"
    assert body["dropped"] == {"inside_cutoff": 1}
    assert api.get("/runs/nope").status_code == 404


@pytest.mark.parametrize("sport", ["NBA", "NCAAB", "NCAAF"])
def test_run_now_starts_a_run(api: Api, sport: str) -> None:
    response = api.send("POST", "/runs", {"sport": sport, "slate_date": str(SLATE)})
    assert response.status_code == 202
    assert api.starter.started == [(sport, SLATE)]


def test_run_now_defaults_to_todays_eastern_date(api: Api) -> None:
    body = api.send("POST", "/runs", {"sport": "NBA"}).json()
    assert body["slate_date"] == "2026-11-28"


def test_run_now_waits_for_a_run_that_is_still_going(api: Api) -> None:
    write_run(api.root, "01AAAA", status="running")
    response = api.send("POST", "/runs", {"sport": "NCAAF", "slate_date": str(SLATE)})
    assert response.status_code == 409
    assert api.starter.started == []


def test_run_now_launches_the_v2_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda command, **_: launched.append(command))
    LocalRunStarter().start("NCAAB", SLATE)
    LocalRunStarter().rebook("NCAAB", SLATE, "T1")
    assert launched[0][1:] == [
        "-m", "hoopsedge.v2.slate", "run", "--sport", "NCAAB", "--date", "2026-11-28",
        "--trigger", "dashboard",
    ]  # fmt: skip
    assert launched[1][3:] == [
        "rebook", "--sport", "NCAAB", "--date", "2026-11-28", "--ticket-id", "T1",
    ]  # fmt: skip


# The review queue and aliases.


def queue(api: Api) -> None:
    """SportyBet's home name matched Delaware; its away name matched nothing."""
    api.table.put_item(
        Item={
            "pk": "UNMATCHED#NCAAF", "sk": "sr:match:9", "home_name": "Delaware Blue Hens",
            "away_name": "Connecticut U Huskies", "start": NOW.isoformat(), "reason": "unmatched",
            "status": "open", "home_team_id": "48",
        }
    )  # fmt: skip


def test_the_queue_lists_open_items_per_sport(api: Api) -> None:
    queue(api)
    [item] = api.get("/unmatched").json()
    assert (item["sport"], item["event_id"]) == ("NCAAF", "sr:match:9")
    assert api.get("/unmatched", sport="NBA").json() == []


def test_resolving_adds_an_alias_for_the_unmatched_side_only(api: Api) -> None:
    queue(api)
    response = api.send("POST", "/unmatched/sr:match:9/resolve", {"team_id_away": "41"})
    assert response.status_code == 200, response.text
    assert response.json()["aliases"] == {"connecticut u huskies": "41"}
    assert api.get("/unmatched").json() == []
    owner = [a for a in api.get("/aliases/NCAAF").json() if a["source"] == "owner"]
    assert owner == [{"alias": "connecticut u huskies", "team_id": "41",
                      "team_name": "UConn Huskies", "source": "owner"}]  # fmt: skip


def test_resolving_needs_every_unmatched_side_and_real_teams(api: Api) -> None:
    queue(api)
    assert api.send("POST", "/unmatched/sr:match:9/resolve", {}).status_code == 422
    bad = {"team_id_away": "999"}
    assert api.send("POST", "/unmatched/sr:match:9/resolve", bad).status_code == 422
    same = {"team_id_away": "48"}
    assert api.send("POST", "/unmatched/sr:match:9/resolve", same).status_code == 422


def test_an_event_can_be_ignored(api: Api) -> None:
    queue(api)
    body = api.send("POST", "/unmatched/sr:match:9/resolve", {"ignore": True}).json()
    assert body["status"] == "ignored" and api.get("/unmatched").json() == []


def test_aliases_list_seeds_and_owner_and_only_owner_ones_delete(api: Api) -> None:
    body = {"sport": "NCAAF", "name": "UConn Huskies!", "team_id": "41"}
    added = api.send("POST", "/aliases", body)
    assert added.status_code == 200 and added.json()["alias"] == "uconn huskies"
    listed = api.get("/aliases/NCAAF").json()
    assert {"alias": "connecticut huskies", "team_id": "41", "team_name": "UConn Huskies",
            "source": "seed"} in listed  # fmt: skip
    assert api.send("DELETE", "/aliases/NCAAF/uconn huskies").status_code == 204
    assert api.send("DELETE", "/aliases/NCAAF/connecticut huskies").status_code == 404
    bad = {"sport": "NCAAF", "name": "Somebody", "team_id": "999"}
    assert api.send("POST", "/aliases", bad).status_code == 422


def test_teams_come_from_the_registry(api: Api) -> None:
    assert [t["team_id"] for t in api.get("/teams/NCAAF").json()] == ["48", "41"]


# Config (D98).


def test_config_starts_as_the_v2_defaults(api: Api) -> None:
    view = api.get("/config").json()
    assert view["version"] == 0
    document = view["document"]
    assert document["sports"]["NCAAF"]["over_threshold"] == 4.0
    assert document["tickets"]["booking_cutoff_minutes"] == 20
    assert document["dry_run"] is True and document["pause_all"] is False
    assert set(document["schedules"]) == set(SCHEDULE_NAMES)


def _with(view: dict[str, Any], change: Any) -> dict[str, Any]:
    document = json.loads(json.dumps(view["document"]))
    change(document)
    return {"version": view["version"], "document": document}


def test_a_config_change_is_a_new_version_with_its_diff(api: Api) -> None:
    view = api.get("/config").json()
    body = _with(view, lambda d: d["sports"]["NCAAB"].update(over_threshold=6.0))
    response = api.send("PUT", "/config", body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["version"] == 1
    assert saved["diff"] == [{"path": "sports.NCAAB.over_threshold", "from": 5.0, "to": 6.0}]
    assert load(api.table)[1].sports["NCAAB"].over_threshold == 6.0  # What runs will use.
    [version] = api.get("/config/versions").json()
    assert version["version"] == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["tickets"].update(booking_cutoff_minutes=10),
        lambda d: d["sports"]["NCAAF"].update(min_games=9),
        lambda d: d["schedules"].pop("nba_slate"),
        lambda d: d["schedules"].update(nba_slate="25:00"),
        lambda d: d.update(surprise=True),
        lambda d: d["sports"]["NCAAF"]["line_mode"].update(totals="riskiest"),
    ],
    ids=["cutoff", "min-games", "missing-schedule", "bad-time", "unknown", "line-mode"],
)
def test_a_config_that_breaks_a_rule_is_refused(api: Api, change: Any) -> None:
    response = api.send("PUT", "/config", _with(api.get("/config").json(), change))
    assert response.status_code == 422
    assert api.get("/config").json()["version"] == 0
    assert api.schedules.updates == []


def test_a_stale_version_is_refused(api: Api) -> None:
    view = api.get("/config").json()
    assert api.send("PUT", "/config", _with(view, lambda d: None)).status_code == 200
    assert api.send("PUT", "/config", _with(view, lambda d: None)).status_code == 409


def test_only_changed_schedules_reach_the_scheduler(api: Api) -> None:
    view = api.get("/config").json()
    api.send("PUT", "/config", _with(view, lambda d: d["schedules"].update(nba_slate="17:00")))
    assert api.schedules.updates == [("nba_slate", "17:00")]
    response = api.send("PUT", "/schedules/ncaaf_slate", {"time": "19:00"})
    assert response.status_code == 200 and response.json()["version"] == 2
    assert api.send("PUT", "/schedules/nightly_learn", {"time": "11:00"}).status_code == 404


def test_a_document_saved_before_v2_keeps_its_switches() -> None:
    old = {"schedules": {"nba_slate": "23:00", "ncaa_slate": "15:00"}, "markets": {},
           "dry_run": False, "pause_all": True}  # fmt: skip
    document = document_from_stored(old)
    assert document.dry_run is False and document.pause_all is True
    assert document.schedules["nba_slate"] == "23:00"
    assert document.sports == default_document().sports


def test_changes_between_finds_nested_differences() -> None:
    assert changes_between({"a": {"b": 1}}, {"a": {"b": 2}}) == [
        {"path": "a.b", "from": 1, "to": 2}
    ]


# Grading and calibration (brief §9, D90).


def test_a_graded_ticket_shows_its_result(api: Api) -> None:
    write_run(api.root, "01AAAA")
    grade_slate(api.root, "NCAAF", SLATE, {"slate": Outcome(FINAL, 30, 20)}, NOW)
    [ticket] = api.get(f"/slates/{SLATE}", sport="NCAAF").json()["sports"][0]["tickets"]
    assert ticket["result"] == "won"
    assert [leg["result"] for leg in ticket["legs"]] == ["won", "won"]


def test_calibration_suggests_and_the_owner_applies(api: Api) -> None:
    write_grades(api.root, season())
    body = api.get("/calibration/NCAAB").json()
    main = next(s for s in body["suggestions"] if (s["market"], s["mode"]) == ("totals", "main"))
    thresholds = main["suggested"]["thresholds"]
    assert thresholds == {"over_threshold": 4.0, "under_threshold": 0.0}
    request = {"market": "totals", "mode": "main", "version": 0, "thresholds": thresholds}

    stale = api.send("POST", "/calibration/NCAAB/apply", {**request, "thresholds": {}})
    assert stale.status_code == 409  # Not what the owner saw: nothing changes.
    assert api.get("/config").json()["version"] == 0

    applied = api.send("POST", "/calibration/NCAAB/apply", request)
    assert applied.status_code == 200
    sport = applied.json()["document"]["sports"]["NCAAB"]
    assert (sport["over_threshold"], sport["under_threshold"]) == (4.0, 0.0)
    assert sport["line_mode"]["totals"] == "main"
    [version] = api.get("/config/versions").json()
    assert version["changed_by"] == "calibration"
    assert api.send("POST", "/calibration/NCAAB/apply", request).status_code == 409  # Old version.


def test_no_suggestion_yet_cant_be_applied(api: Api) -> None:
    request = {"market": "totals", "mode": "safest", "version": 0, "thresholds": {}}
    assert api.send("POST", "/calibration/NCAAB/apply", request).status_code == 404


# System.


def test_the_system_page_shows_relay_freshness_and_switches(api: Api) -> None:
    write_run(api.root, "01AAAA")
    body = api.get("/system").json()
    assert body["relay"]["reachable"] is True
    assert body["freshness"]["ncaaf_run"] == NOW.isoformat()
    assert body["dry_run"] is True and body["scheduled_dry_run"] is True
    assert body["pause_all"] is False
    # B7: today's runs fall after the three full days, so none of them is clean yet.
    golive = body["golive"]
    assert golive["days_needed"] == 3 and len(golive["days"]) == 3
    assert golive["practice_clean"] is False and golive["ready"] is False
    assert golive["live_codes"] == {"NBA": None, "NCAAB": None, "NCAAF": None}


def test_the_system_page_shows_a_supervised_real_code(api: Api) -> None:
    write_run(api.root, "01BBBB", booked=True)
    live = api.get("/system").json()["golive"]["live_codes"]
    assert live["NCAAF"]["code"] == "REALBB" and live["NCAAF"]["run_id"] == "01BBBB"
    assert live["NBA"] is None


# The published schema.


def test_the_published_schema_matches_the_code(tmp_path: Path) -> None:
    # The dashboard's TypeScript types are generated from this file, so drift fails here.
    fresh = export_openapi(tmp_path / "openapi.json")
    assert fresh == json.loads(OPENAPI_FILE.read_text("utf-8")), (
        "Run: docker compose run --rm dev python -m hoopsedge.api.main export"
    )


def test_the_schema_lists_the_v2_sports() -> None:
    document = openapi_document(create_app(Deps(None, Artifacts(Path()), FakeStarter())))
    assert document["components"]["schemas"]["Sport"]["enum"] == ["NBA", "NCAAB", "NCAAF"]
