"""The API after the v1 model left (D96): the secret, leagues, Run now, v2 settings, retired routes.

The read endpoints over v1 tickets (slates, tickets, games, track record) keep their code until
B4 rebuilds the API on v2 data; their tests built fixtures with the v1 writers, so they went to
`legacy/tests` with them.
"""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from hoopsedge.api.app import Deps, create_app, openapi_document
from hoopsedge.api.main import OPENAPI_FILE, LocalRunStarter, export_openapi
from hoopsedge.api.settings import (
    SCHEDULE_NAMES,
    changes_between,
    default_settings,
    v2_document,
)
from hoopsedge.api.store import ApiStore, Artifacts
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.pipeline.schedule import due_jobs, run_due

KEY = "test-secret-value"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
SLATE = date(2026, 11, 24)


class FakeStarter:
    def __init__(self) -> None:
        self.started: list[tuple[str, date, str]] = []

    def start(self, league: str, slate: date, run_id: str) -> None:
        self.started.append((league, slate, run_id))


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
        self.clock = NOW
        self.secret: str | None = KEY
        self.deps = Deps(
            self.store,
            Artifacts(root),
            self.starter,
            self.schedules,
            secret=lambda: self.secret,
            now=lambda: self.clock,
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


ALL_GETS = ["/health/details", f"/slates/{SLATE}", "/runs", "/unmatched", "/config", "/system"]
ALL_MUTATIONS = [
    ("POST", "/runs"), ("PUT", "/config"), ("PUT", "/schedules/nba_slate"),
    ("POST", "/models/NBA/rollback"),
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


def test_health_needs_no_secret_and_says_nothing_else(api: Api) -> None:
    assert api.client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("path", ALL_GETS)
def test_every_read_still_works(api: Api, path: str) -> None:
    assert api.get(path).status_code == 200, path


# Leagues and Run now (D87, D96).


@pytest.mark.parametrize("league", ["NBA", "NCAAB", "NCAAF"])
def test_run_now_starts_a_run_for_every_v2_sport(api: Api, league: str) -> None:
    response = api.send("POST", "/runs", {"league": league, "slate_date": str(SLATE)})
    assert response.status_code == 202
    assert api.starter.started == [(league, SLATE, response.json()["run_id"])]


def test_the_old_league_name_is_refused(api: Api) -> None:
    assert api.send("POST", "/runs", {"league": "NCAA"}).status_code == 422


def test_run_now_launches_the_v2_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda command, **_: launched.append(command))
    LocalRunStarter().start("NCAAB", SLATE, "ignored")
    assert launched[0][1:] == [
        "-m", "hoopsedge.v2.slate", "run", "--sport", "NCAAB", "--date", "2026-11-24",
    ]  # fmt: skip


def test_model_rollback_is_retired(api: Api) -> None:
    response = api.send("POST", "/models/NBA/rollback", {"version": "v1"})
    assert response.status_code == 410


# Settings (cut down for v2).


def test_settings_default_to_v2_schedules_and_dry_run(api: Api) -> None:
    view = api.get("/config").json()
    assert view["version"] == 0
    document = view["document"]
    assert document["dry_run"] is True and document["pause_all"] is False
    assert document["schedules"]["ncaab_slate"] == "16:00"
    assert document["schedules"]["ncaaf_saturday_slate"] == "14:00"
    assert set(document["schedules"]) == set(SCHEDULE_NAMES)


def test_a_settings_document_saved_by_v1_still_loads(api: Api) -> None:
    v1 = {
        **default_settings(),
        "schedules": {"ncaa_slate": "15:00", "nba_slate": "23:00", "nightly_learn": "11:00"},
        "markets": {"NBA": {}},
        "min_legs": 2,
        "dry_run": False,
    }
    api.store.save_settings(v1, 1, [], NOW)
    view = api.get("/config").json()
    assert view["version"] == 1
    assert view["document"]["dry_run"] is False  # Kept: a v2 field.
    assert "markets" not in view["document"]  # Dropped: v1 only.
    assert view["document"]["schedules"]["nba_slate"] == "23:00"  # Same name, kept.
    assert "ncaa_slate" not in view["document"]["schedules"]


def test_v2_document_keeps_only_v2_fields() -> None:
    document = v2_document({"schedules": {"backup": "06:00"}, "allow_alt_lines": True})
    assert document["schedules"]["backup"] == "06:00"
    assert "allow_alt_lines" not in document


def test_only_changed_schedules_reach_the_scheduler(api: Api) -> None:
    view = api.get("/config").json()
    schedules = {**view["document"]["schedules"], "nba_slate": "17:00"}
    body = {"version": 0, "document": {**view["document"], "schedules": schedules}}
    assert api.send("PUT", "/config", body).status_code == 200
    assert api.schedules.updates == [("nba_slate", "17:00")]


@pytest.mark.parametrize(
    "changes",
    [{"schedules": {"nba_slate": "16:00"}}, {"markets": {}}, {"min_legs": 2}],
    ids=["missing-schedules", "v1-markets", "v1-min-legs"],
)
def test_settings_of_the_wrong_shape_are_refused(api: Api, changes: dict[str, Any]) -> None:
    view = api.get("/config").json()
    body = {"version": 0, "document": {**view["document"], **changes}}
    assert api.send("PUT", "/config", body).status_code == 422
    assert api.schedules.updates == []


def test_a_schedule_change_reaches_the_scheduler(api: Api) -> None:
    response = api.send("PUT", "/schedules/ncaaf_slate", {"time": "19:00"})
    assert response.status_code == 200, response.text
    assert ("ncaaf_slate", "19:00") in api.schedules.updates
    assert api.send("PUT", "/schedules/nightly_learn", {"time": "11:00"}).status_code == 404


def test_changes_between_finds_nested_differences() -> None:
    assert changes_between({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}}) == [
        {"path": "a.c", "from": 2, "to": 3}
    ]


# The scheduler (brief §12).


def _names(now: datetime, done: dict[str, str] | None = None, **times: str) -> set[str]:
    return {job.name for job in due_jobs(now, done or {}, times or None)}


def test_basketball_slates_run_at_16_and_21_lagos() -> None:
    assert _names(datetime(2026, 11, 24, 15, 0, tzinfo=UTC)) == {"ncaab_slate", "nba_slate"}
    assert _names(datetime(2026, 11, 24, 20, 0, tzinfo=UTC)) == {
        "ncaab_late_slate", "nba_late_slate",
    }  # fmt: skip


def test_ncaaf_runs_at_18_daily_and_also_14_on_saturdays() -> None:
    tuesday_14, saturday_14 = (datetime(2026, 11, d, 13, 0, tzinfo=UTC) for d in (24, 28))
    assert "ncaaf_saturday_slate" not in _names(tuesday_14)
    assert "ncaaf_saturday_slate" in _names(saturday_14)
    assert "ncaaf_slate" in _names(datetime(2026, 11, 24, 17, 0, tzinfo=UTC))


def test_a_slate_job_runs_the_v2_slate_for_the_eastern_date() -> None:
    late = datetime(2026, 11, 25, 3, 30, tzinfo=UTC)  # 04:30 Lagos, still the 24th in the US.
    [job] = due_jobs(late, {}, {"nba_late_slate": "04:00"})
    assert job.command[-6:] == (
        "hoopsedge.v2.slate", "run", "--sport", "NBA", "--date", "2026-11-24",
    )  # fmt: skip


def test_v1_jobs_are_gone() -> None:
    every = {
        job.name
        for hour in range(24)
        for job in due_jobs(datetime(2026, 11, 28, hour, 0, tzinfo=UTC), {})
    }
    assert not any(name.startswith(("learn", "grade", "slate-")) for name in every)


def test_pause_all_stops_scheduled_jobs_and_a_new_time_moves_them(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    assert run_due(tmp_path, now, lambda *_, **__: None, {"pause_all": True}) == []
    assert _names(now, nba_slate="17:00") == {"ncaab_slate"}
    assert _names(now + timedelta(hours=1), nba_slate="17:00") == {
        "ncaab_slate", "nba_slate",
    }  # fmt: skip


# The published schema.


def test_the_published_schema_matches_the_code(tmp_path: Path) -> None:
    # The dashboard's TypeScript types are generated from this file, so drift fails here.
    fresh = export_openapi(tmp_path / "openapi.json")
    assert fresh == json.loads(OPENAPI_FILE.read_text("utf-8")), (
        "Run: docker compose run --rm dev python -m hoopsedge.api.main export"
    )


def test_the_schema_lists_the_v2_leagues() -> None:
    document = openapi_document(create_app(Deps(None, Artifacts(Path()), FakeStarter())))
    assert document["components"]["schemas"]["League"]["enum"] == ["NBA", "NCAAB", "NCAAF"]
