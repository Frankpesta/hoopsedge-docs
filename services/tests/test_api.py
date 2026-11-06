"""The API (docs/09, D8, D72, C16 to C18): the secret, every endpoint and every mutation rule."""

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from hoopsedge.api.app import Deps, create_app, openapi_document
from hoopsedge.api.main import OPENAPI_FILE, export_openapi
from hoopsedge.api.schemas import UNPROVEN_NOTE
from hoopsedge.api.settings import changes_between, default_settings
from hoopsedge.api.store import ApiStore, Artifacts
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.engine import Leg, Ticket
from hoopsedge.engine.config import apply_settings
from hoopsedge.engine.schemas import Rejection
from hoopsedge.features.registry import FEATURE_SET_VERSION
from hoopsedge.grading.run import Finals, grade_pass
from hoopsedge.grading.settle import Final
from hoopsedge.pipeline.labels import leg_summary, pick_label
from hoopsedge.pipeline.schedule import due_jobs, run_due
from hoopsedge.pipeline.store import DRY_RUN, PipelineStore
from tests.engine_data import config, prediction

KEY = "test-secret-value"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TIP = NOW + timedelta(hours=8)
SLATE = date(2026, 11, 7)
NAMES = {"g1": ("Duke Blue Devils", "UNC Tar Heels"), "g2": ("Kansas Jayhawks", "Baylor Bears")}


def _leg(rank: int = 1, game: str = "g1", **changes: Any) -> Leg:
    fields = {
        "rank": rank, "league": "NCAA", "game_id": game, "event_id": f"e-{game}",
        "family": "total_ft", "market_id": "225", "specifier": "total=189.5", "line": 189.5,
        "team": None, "side": "over", "outcome_id": "12", "odds": 2.0, "p_raw": 0.61,
        "p_calibrated": 0.6, "p_win": 0.6, "p_push": 0.0, "p_lose": 0.4, "fair": 0.5, "edge": 0.1,
        "ev": 0.2, "tip_time": TIP, "model_version": "v", "feature_hash": "h",
        "offer_fetched_at": NOW, "predicted_mean": 185.0,
    }  # fmt: skip
    return Leg.model_validate({**fields, **changes})


def _ticket(
    legs: list[Leg], family: str = "total_ft", label: str = "measured", number: int = 1
) -> Ticket:
    return Ticket.model_validate(
        {
            "league": "NCAA", "family": family, "ticket_no": number, "legs": tuple(legs),
            "combined_odds": 4.0, "combined_prob": 0.36, "expected_value": 0.44,
            "selection_hash": "hash", "passed_gate": label == "measured", "edge_label": label,
        }
    )  # fmt: skip


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
        self.pipeline = PipelineStore(table)
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

    def send(self, method: str, path: str, body: Any = None, key: str | None = None) -> Any:
        self._keys += 1
        headers = {"x-hoopsedge-key": KEY, "idempotency-key": key or f"key-{self._keys}"}
        return self.client.request(method, path, headers=headers, json=body)

    def save(self, ticket_id: str, ticket: Ticket, version: int = 1, status: str = DRY_RUN) -> None:
        self.pipeline.save_ticket(
            ticket_id, ticket, "run1", SLATE, version, status, "DRYRUN-1", names=NAMES
        )

    def game(self, game: str = "g1", home: str = "h", away: str = "a", **fields: Any) -> None:
        self.table.put_item(
            Item={
                "pk": f"GAME#NCAA#{game}",
                "sk": "META",
                "gsi1pk": f"SLATE#NCAA#{SLATE}",
                "gsi1sk": TIP.isoformat(),
                "league": "NCAA",
                "game_id": game,
                "tip_utc": TIP.isoformat(),
                "home_id": home,
                "away_id": away,
                **fields,
            }
        )

    def teams(self) -> None:
        self.pipeline.sync_teams("NCAA", {"h": "Duke Blue Devils", "a": "UNC Tar Heels"})


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


ALL_GETS = [
    "/health/details", f"/slates/{SLATE}", "/tickets/x", "/games/x", "/runs", "/runs/x",
    "/track-record", "/models/NBA", "/evidence/NBA", "/unmatched", "/teams/NCAA", "/config",
    "/system",
]  # fmt: skip
ALL_MUTATIONS = [
    ("POST", "/unmatched/x/resolve"), ("POST", "/runs"), ("PUT", "/config"),
    ("PUT", "/schedules/nba_slate"), ("POST", "/models/NBA/rollback"),
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


def test_a_refusal_leaks_nothing_and_logs_the_source(api: Api, caplog: Any) -> None:
    response = api.client.get("/config", headers={"x-hoopsedge-key": "wrong"})
    assert response.json() == {"detail": "Unauthorized"}
    assert "Refused a request from" in caplog.text
    assert "wrong" not in caplog.text


@pytest.mark.parametrize("secret", [None, ""])
def test_no_secret_configured_lets_nobody_in(api: Api, secret: str | None) -> None:
    api.secret = secret
    assert api.client.get("/config", headers={"x-hoopsedge-key": ""}).status_code == 401
    assert api.client.get("/config", headers={"x-hoopsedge-key": KEY}).status_code == 401


def test_health_needs_no_secret_and_says_nothing_else(api: Api) -> None:
    response = api.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}  # C17.


def test_the_schema_and_docs_are_not_served(api: Api) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.client.get(path, headers={"x-hoopsedge-key": KEY}).status_code == 404


def test_reads_are_cached_briefly_and_refusals_are_not(api: Api) -> None:
    assert api.get("/config").headers["Cache-Control"] == "private, max-age=30"
    assert api.client.get("/config").headers["Cache-Control"] == "no-store"
    assert (
        api.send("PUT", "/schedules/nope", {"time": "10:00"}).headers["Cache-Control"] == "no-store"
    )


# Slates and tickets.


def test_a_slate_groups_the_latest_tickets_by_market(api: Api) -> None:
    api.game("g1")
    api.game("g2")
    api.save("old", _ticket([_leg()]), version=1)
    api.save("new", _ticket([_leg(1, "g1"), _leg(2, "g2")]), version=2)
    api.pipeline.supersede("NCAA", SLATE, 2)
    api.pipeline.save_run(
        "run1",
        {
            "league": "NCAA", "slate_date": SLATE.isoformat(), "status": "succeeded",
            "started_at": NOW.isoformat(),
            "counts": {"LOW_PROB": 9, "UNMATCHED": 1, "unmatched_events": 1, "predictions": 2},
        },
    )  # fmt: skip
    body = api.get(f"/slates/{SLATE}").json()
    ncaa = next(item for item in body["leagues"] if item["league"] == "NCAA")
    assert [card["ticket_id"] for card in ncaa["markets"]["total_ft"]] == ["new"]
    assert ncaa["markets"]["handicap"] == []
    assert ncaa["games"] == 2
    assert ncaa["first_tip"] == TIP.isoformat()
    assert ncaa["run"]["status"] == "succeeded"
    assert ncaa["excluded"] == {"LOW_PROB": 9, "UNMATCHED": 1}
    assert ncaa["eligible_legs"] == 2
    assert ncaa["unmatched_events"] == 1
    card = ncaa["markets"]["total_ft"][0]
    assert card["legs"] == 2
    assert card["legs_summary"][0]["matchup"] == "UNC Tar Heels at Duke Blue Devils"
    assert card["legs_summary"][0]["pick"] == "Over 189.5"


def test_an_empty_slate_is_valid_not_an_error(api: Api) -> None:
    response = api.get(f"/slates/{SLATE}")
    assert response.status_code == 200
    assert all(league["run"] is None for league in response.json()["leagues"])


def test_a_bad_date_is_explained(api: Api) -> None:
    assert api.get("/slates/yesterday").status_code == 422
    assert api.get("/track-record", **{"from": "soon"}).status_code == 422


def test_a_measured_ticket_shows_its_numbers(api: Api) -> None:
    api.save("t1", _ticket([_leg()]))
    card = api.get("/tickets/t1").json()["ticket"]
    assert card["combined_prob"] == pytest.approx(0.36)
    assert card["expected_value"] == pytest.approx(0.44)
    assert card["probability_note"] is None
    assert card["legs_summary"][0]["p_calibrated"] == pytest.approx(0.6)


def test_an_unproven_ticket_never_returns_a_probability_or_ev(api: Api) -> None:
    # D72: enforced here, so no dashboard mistake can show "1 in 500" on these markets.
    api.game("g1")
    half = _leg(family="total_1h", market_id="68", line=75.5, specifier="total=75.5")
    api.save("t1", _ticket([half], "total_1h", "unproven"))
    body = api.get("/tickets/t1").json()
    card = body["ticket"]
    assert card["combined_prob"] is None
    assert card["expected_value"] is None
    assert card["probability_note"] == UNPROVEN_NOTE
    assert card["passed_gate"] is False
    assert card["legs_summary"][0]["p_calibrated"] is None
    assert card["legs_summary"][0]["edge"] is None
    assert card["legs_summary"][0]["odds"] == 2.0
    leg = body["leg_details"][0]
    for name in ("p_raw", "p_calibrated", "p_win", "p_lose", "edge", "ev"):
        assert leg[name] is None, name
    slate_card = next(
        item for item in api.get(f"/slates/{SLATE}").json()["leagues"] if item["league"] == "NCAA"
    )["markets"]["total_1h"][0]
    assert slate_card["combined_prob"] is None
    text = json.dumps(body["ticket"])
    assert "0.36" not in text
    assert "0.44" not in text


def test_a_ticket_shows_its_code_legs_and_version_history(api: Api) -> None:
    api.save("v1", _ticket([_leg()]), version=1)
    api.save("v2", _ticket([_leg()]), version=2)
    body = api.get("/tickets/v2").json()
    assert body["ticket"]["booking_code"] == "DRYRUN-1"
    assert [row["ticket_id"] for row in body["versions"]] == ["v1", "v2"]
    assert body["leg_details"][0]["status"] == "pending"
    assert body["leg_details"][0]["pick"] == "Over 189.5"
    assert body["selection_hash"] == "hash"


def test_a_settled_ticket_shows_its_results(api: Api) -> None:
    api.save("t1", _ticket([_leg()]))
    grade_pass(api.pipeline, Finals({"g1": Final(100, 90, 50, 44)}, {}), NOW)
    body = api.get("/tickets/t1").json()
    assert body["ticket"]["settle_status"] == "won"
    assert body["settled_combined_odds"] == 2
    leg = body["leg_details"][0]
    assert (leg["status"], leg["actual_value"], leg["model_error"]) == ("won", 190, 5)


@pytest.mark.parametrize("path", ["/tickets/none", "/games/none", "/runs/none"])
def test_what_does_not_exist_is_a_404(api: Api, path: str) -> None:
    assert api.get(path).status_code == 404


def test_an_odd_id_never_reaches_the_database(api: Api) -> None:
    assert api.get("/tickets/a%23b").status_code == 422


# Games.


def _detail(api: Api, label: str = "measured", family: str = "total_ft") -> None:
    api.game("g1", final_field=None, home_pts=80, away_pts=70)
    api.teams()
    injuries = [{"player_id": "p", "team_id": "h", "status": "Out"}]
    base = prediction("g1")
    assert base.injury is not None
    predicted = base.model_copy(
        update={
            "key_features": {"adj_o_h": 110.0, "rest_a": None},
            "injury": base.injury.model_copy(update={"listed": tuple(injuries)}),
        }
    )
    api.pipeline.save_prediction("run1", predicted)
    leg = _leg(family=family, market_id="225")
    rejection = Rejection.model_validate(
        {
            "league": "NCAA", "game_id": "g1", "event_id": "e", "family": family,
            "reason": "LOW_PROB", "p_calibrated": 0.51, "edge": 0.01, "side": "under",
        }
    )  # fmt: skip
    api.pipeline.save_game_detail("run1", "NCAA", "g1", [], [rejection], [leg], {family: label})


def test_a_game_page_has_everything_in_one_call(api: Api) -> None:
    _detail(api)
    body = api.get("/games/g1").json()
    assert body["home_name"] == "Duke Blue Devils"
    assert body["final"] == {"home": 80, "away": 70}
    assert body["prediction"]["ft"]["mu_home"] == 110
    assert body["key_features"]["adj_o_h"] == 110
    assert body["injuries"][0]["status"] == "Out"
    assert body["rejections"][0]["p_calibrated"] == pytest.approx(0.51)
    assert body["legs"][0]["edge"] == pytest.approx(0.1)


def test_an_unproven_market_hides_probabilities_on_the_game_page_too(api: Api) -> None:
    _detail(api, "unproven", "total_1h")
    body = api.get("/games/g1").json()
    assert "p_calibrated" not in body["rejections"][0]
    assert "edge" not in body["legs"][0]
    assert body["legs"][0]["probability_note"] == UNPROVEN_NOTE
    assert body["prediction"]["ft"]["mu_home"] == 110  # The score forecast itself stays.


def test_a_game_with_no_run_yet_still_loads(api: Api) -> None:
    api.game("g1")
    body = api.get("/games/g1").json()
    assert body["prediction"] is None
    assert body["final"] is None
    assert body["lines"] == []


# Runs.


def _run(api: Api, run_id: str, at: datetime, **fields: Any) -> None:
    api.pipeline.save_run(
        run_id,
        {
            "league": "NCAA", "slate_date": SLATE.isoformat(), "status": "failed",
            "started_at": at.isoformat(), **fields,
        },
    )  # fmt: skip


def test_runs_come_newest_first_with_a_limit(api: Api) -> None:
    for number in range(3):
        _run(api, f"r{number}", NOW + timedelta(minutes=number))
    assert [row["run_id"] for row in api.get("/runs", limit=2).json()] == ["r2", "r1"]
    assert api.get("/runs", limit=0).status_code == 422
    assert api.get("/runs", limit=500).status_code == 422


def test_a_run_shows_steps_errors_and_dropped_legs(api: Api) -> None:
    _run(api, "r1", NOW, error="BlockedError: 403", steps=[{"name": "FetchOffers"}])
    api.pipeline.log("r1", NOW, "RecheckOffers", "LEG_DROPPED", {"game_id": "g1"})
    body = api.get("/runs/r1").json()
    assert body["run"]["error"] == "BlockedError: 403"
    assert body["steps"] == [{"name": "FetchOffers"}]
    assert body["log"][0]["event"] == "LEG_DROPPED"


# Track record.


def _graded(api: Api) -> None:
    api.save(
        "t1",
        _ticket(
            [
                _leg(1, "g1", p_calibrated=0.62),
                _leg(2, "g2", side="under", p_calibrated=0.61),
                _leg(3, "g3", line=190.0, specifier="total=190.0", p_calibrated=0.71),
            ]
        ),
    )
    final = Final(100, 90, 50, 44)
    grade_pass(api.pipeline, Finals({"g1": final, "g2": final, "g3": final}, {}), NOW)


def test_the_track_record_comes_from_stored_aggregates(api: Api) -> None:
    _graded(api)
    body = api.get("/track-record", league="NCAA", market="total_ft").json()
    assert (body["legs"], body["won"], body["lost"], body["push"]) == (3, 1, 1, 1)
    assert body["hit_rate"] == pytest.approx(0.5)
    assert body["push_rate"] == pytest.approx(1 / 3)
    assert body["breakeven_hit_rate"] == pytest.approx(0.5)  # Everything at 2.0.
    assert body["flat_stake_profit"] == pytest.approx(0.0)
    assert body["flat_stake_roi"] == pytest.approx(0.0)
    assert body["tickets"]["lost"] == 1
    assert body["missed_by"] == {"1": 1}
    assert (body["legs_won_on_lost"], body["legs_on_lost"]) == (1, 3)
    bucket = next(item for item in body["buckets"] if item["bucket"] == 0.6)
    assert bucket["legs"] == 2
    assert bucket["hit_rate"] == pytest.approx(0.5)
    assert bucket["mean_probability"] == pytest.approx(0.615)
    assert body["calibration_error"] == pytest.approx(0.115)
    assert body["daily"] == [{"date": str(SLATE), "legs": 3, "won": 1, "lost": 1}]


def test_the_track_record_filters_by_date_league_and_market(api: Api) -> None:
    _graded(api)
    assert api.get("/track-record").json()["legs"] == 3
    assert api.get("/track-record", league="NBA").json()["legs"] == 0
    assert api.get("/track-record", market="handicap").json()["legs"] == 0
    assert api.get("/track-record", **{"from": "2026-11-08"}).json()["legs"] == 0
    assert api.get("/track-record", to="2026-11-06").json()["legs"] == 0
    assert (
        api.get("/track-record", **{"from": "2026-11-07", "to": "2026-11-07"}).json()["legs"] == 3
    )


def test_an_empty_track_record_has_no_invented_rates(api: Api) -> None:
    body = api.get("/track-record").json()
    assert body["hit_rate"] is None
    assert body["flat_stake_roi"] is None
    assert body["calibration_error"] is None


def test_a_superseded_ticket_does_not_inflate_the_record(api: Api) -> None:
    api.save("old", _ticket([_leg()]), version=1)
    api.save("new", _ticket([_leg()]), version=2)
    api.pipeline.supersede("NCAA", SLATE, 2)
    grade_pass(api.pipeline, Finals({"g1": Final(100, 90, 50, 44)}, {}), NOW)
    assert api.get("/track-record").json()["legs"] == 1


# Models and evidence.


def _model(root: Path, version: str, league: str = "NBA") -> None:
    folder = root / "models" / league / version
    (folder / "calibrators").mkdir(parents=True)
    (folder / "periods.json").write_text("{}", "utf-8")
    (folder / "feature_set_version.txt").write_text(FEATURE_SET_VERSION, "utf-8")
    (folder / "evidence.json").write_text(json.dumps({"gates": {"handicap": "x"}}), "utf-8")


def _registry(api: Api) -> Any:
    from hoopsedge.learning.registry import ModelRegistry

    return ModelRegistry(api.table, api.root / "models")


def test_models_show_the_champion_decisions_and_learning_curve(api: Api) -> None:
    _model(api.root, "20261106-1000-aaaaaaa")
    registry = _registry(api)
    registry.bootstrap("NBA", NOW)
    metrics = {"log_likelihood_gain": {"mean": 0.01}, "crps_gain": {"mean": 0.02}, "games": 200}
    registry.audit(
        "NBA", NOW + timedelta(days=1), "20261106-1000-aaaaaaa", "rejected",
        {"metrics": metrics, "reasons": ["Not better"]},
    )  # fmt: skip
    report = api.root / "reports/learning/NBA"
    report.mkdir(parents=True)
    (report / "2026-11-07.json").write_text(json.dumps({"drift": {"flags": ["bias"]}}), "utf-8")
    body = api.get("/models/NBA").json()
    assert body["champion"]["version"] == "20261106-1000-aaaaaaa"
    assert [row["decision"] for row in body["learning_curve"]] == ["bootstrap", "rejected"]
    point = body["learning_curve"][1]
    assert (point["log_likelihood_gain"], point["crps_gain"], point["games"]) == (0.01, 0.02, 200)
    assert point["reasons"] == ["Not better"]
    assert body["drift"] == {"flags": ["bias"]}
    assert set(body["gates"]) == {"handicap", "total_ft", "total_1h", "team_total"}
    assert body["gates"]["total_ft"]["passed"] is False


def test_evidence_comes_from_the_champions_own_folder(api: Api) -> None:
    assert api.get("/evidence/NBA").json()["evidence"] is None
    _model(api.root, "20261106-1000-aaaaaaa")
    _registry(api).bootstrap("NBA", NOW)
    body = api.get("/evidence/NBA").json()
    assert body["version"] == "20261106-1000-aaaaaaa"
    assert body["evidence"] == {"gates": {"handicap": "x"}}


def test_an_unknown_league_is_refused(api: Api) -> None:
    assert api.get("/models/EPL").status_code == 422


def test_a_rollback_repoints_the_champion_and_is_audited(api: Api) -> None:
    _model(api.root, "20261106-1000-aaaaaaa")
    _model(api.root, "20261107-1000-bbbbbbb")
    _registry(api).bootstrap("NBA", NOW)
    response = api.send(
        "POST", "/models/NBA/rollback", {"version": "20261106-1000-aaaaaaa", "reason": "Off"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == "20261106-1000-aaaaaaa"
    assert _registry(api).require("NBA").version == "20261106-1000-aaaaaaa"
    assert any(row["decision"] == "rollback" for row in api.get("/models/NBA").json()["history"])


def test_a_rollback_to_nothing_is_refused_and_changes_nothing(api: Api) -> None:
    _model(api.root, "20261107-1000-bbbbbbb")
    _registry(api).bootstrap("NBA", NOW)
    response = api.send("POST", "/models/NBA/rollback", {"version": "20260101-0000-nothere"})
    assert response.status_code == 422
    assert _registry(api).require("NBA").version == "20261107-1000-bbbbbbb"


# Unmatched.


def _unmatched(api: Api, event: str = "sr:match:1") -> None:
    api.table.put_item(
        Item={
            "pk": "UNMATCHED#NCAA", "sk": event, "home_name": "Mystery Team",
            "away_name": "UNC Tar Heels", "start": TIP.isoformat(), "reason": "team_not_resolved",
            "home_suggestions": [{"team_id": "h", "score": 80}], "away_suggestions": [],
            "status": "open",
        }
    )  # fmt: skip


def test_open_unmatched_events_are_listed_with_suggestions(api: Api) -> None:
    _unmatched(api)
    body = api.get("/unmatched").json()
    assert body[0]["event_id"] == "sr:match:1"
    assert body[0]["league"] == "NCAA"
    assert body[0]["home_suggestions"][0]["team_id"] == "h"


def test_resolving_writes_aliases_the_matcher_will_use(api: Api) -> None:
    _unmatched(api)
    api.teams()
    response = api.send(
        "POST", "/unmatched/sr:match:1/resolve", {"team_id_home": "h", "team_id_away": "a"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["aliases"] == {"mystery team": "h", "unc tar heels": "a"}
    assert api.get("/unmatched").json() == []
    # The pipeline's own reader finds them, so the same name matches from then on.
    assert {"alias_normalized": "mystery team", "team_id": "h"} in api.pipeline.confirmed_aliases(
        "NCAA"
    )
    item = api.table.get_item(Key={"pk": "UNMATCHED#NCAA", "sk": "sr:match:1"})["Item"]
    assert item["status"] == "resolved"
    assert int(item["ttl"]) == int((NOW + timedelta(days=14)).timestamp())


def test_an_event_can_be_ignored_without_touching_aliases(api: Api) -> None:
    _unmatched(api)
    response = api.send("POST", "/unmatched/sr:match:1/resolve", {"ignore": True})
    assert response.json()["status"] == "ignored"
    assert api.pipeline.confirmed_aliases("NCAA") == []
    assert api.get("/unmatched").json() == []


@pytest.mark.parametrize(
    "body",
    [
        {},  # Nothing chosen.
        {"team_id_home": "h"},  # Only one team.
        {"team_id_home": "h", "team_id_away": "h"},  # The same team twice.
        {"team_id_home": "h", "team_id_away": "unknown"},  # Not a real team.
    ],
)
def test_a_bad_resolution_is_refused_and_writes_nothing(api: Api, body: dict[str, Any]) -> None:
    _unmatched(api)
    api.teams()
    assert api.send("POST", "/unmatched/sr:match:1/resolve", body).status_code == 422
    assert api.pipeline.confirmed_aliases("NCAA") == []
    assert len(api.get("/unmatched").json()) == 1


def test_resolving_an_unknown_event_is_a_404(api: Api) -> None:
    assert api.send("POST", "/unmatched/none/resolve", {"ignore": True}).status_code == 404


def test_teams_are_listed_by_name(api: Api) -> None:
    api.teams()
    assert api.get("/teams/NCAA").json() == [
        {"team_id": "h", "name": "Duke Blue Devils"},
        {"team_id": "a", "name": "UNC Tar Heels"},
    ]


# Run now.


def test_run_now_starts_a_run_and_returns_its_id(api: Api) -> None:
    response = api.send("POST", "/runs", {"league": "NBA", "slate_date": "2026-11-07"})
    assert response.status_code == 202
    body = response.json()
    assert body["league"] == "NBA"
    assert api.starter.started == [("NBA", SLATE, body["run_id"])]


def test_run_now_defaults_to_todays_eastern_date(api: Api) -> None:
    api.clock = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)  # Still the 6th in the US.
    assert api.send("POST", "/runs", {"league": "NBA"}).json()["slate_date"] == "2026-11-06"


def test_run_now_is_refused_while_the_lock_is_held(api: Api) -> None:
    assert api.pipeline.acquire_lock("NBA", SLATE, "other", NOW)
    response = api.send("POST", "/runs", {"league": "NBA", "slate_date": str(SLATE)})
    assert response.status_code == 409
    assert api.starter.started == []


def test_an_expired_lock_does_not_block_run_now(api: Api) -> None:
    assert api.pipeline.acquire_lock("NBA", SLATE, "crashed", NOW - timedelta(hours=1))
    assert api.send("POST", "/runs", {"league": "NBA", "slate_date": str(SLATE)}).status_code == 202


# Mutation rules (C16, C18).


@pytest.mark.parametrize(("method", "path"), ALL_MUTATIONS)
def test_every_mutation_needs_an_idempotency_key(api: Api, method: str, path: str) -> None:
    response = api.client.request(method, path, json={}, headers={"x-hoopsedge-key": KEY})
    assert response.status_code in {400, 422}
    assert api.starter.started == []


def test_the_same_key_gets_the_first_answer_and_does_nothing_twice(api: Api) -> None:
    body = {"league": "NBA", "slate_date": str(SLATE)}
    first = api.send("POST", "/runs", body, key="same")
    second = api.send("POST", "/runs", body, key="same")
    assert second.status_code == first.status_code == 202
    assert second.json() == first.json()
    assert len(api.starter.started) == 1


def test_a_failed_request_may_try_again_with_its_key(api: Api) -> None:
    body = {"league": "NBA", "slate_date": str(SLATE)}
    assert api.pipeline.acquire_lock("NBA", SLATE, "other", NOW)
    assert api.send("POST", "/runs", body, key="same").status_code == 409
    api.pipeline.release_lock("NBA", SLATE, "other")
    assert api.send("POST", "/runs", body, key="same").status_code == 202


def test_a_request_still_in_progress_cannot_run_twice(api: Api) -> None:
    api.store.claim_idempotency("busy", f"run:NBA:{SLATE}", NOW)
    body = {"league": "NBA", "slate_date": str(SLATE)}
    assert api.send("POST", "/runs", body, key="busy").status_code == 409
    assert api.starter.started == []


def test_mutations_are_limited_to_ten_a_minute_across_processes(api: Api) -> None:
    for number in range(10):
        response = api.send("PUT", "/schedules/nba_slate", {"time": f"1{number}:00"})
        assert response.status_code == 200, response.text
    limited = api.send("PUT", "/schedules/nba_slate", {"time": "20:00"})
    assert limited.status_code == 429
    assert api.get("/config").json()["document"]["schedules"]["nba_slate"] == "19:00"
    # A second app on the same table sees the same count: an in-memory limit wouldn't.
    other = TestClient(create_app(api.deps))
    headers = {"x-hoopsedge-key": KEY, "idempotency-key": "other"}
    assert (
        other.put("/schedules/nba_slate", json={"time": "21:00"}, headers=headers).status_code
        == 429
    )
    api.clock += timedelta(minutes=1)
    assert api.send("PUT", "/schedules/nba_slate", {"time": "20:00"}).status_code == 200


def test_reads_are_never_rate_limited(api: Api) -> None:
    for _ in range(15):
        assert api.get("/config").status_code == 200


# Settings.


def test_settings_start_from_what_phase_four_decided(api: Api) -> None:
    body = api.get("/config").json()
    assert body["version"] == 0
    document = body["document"]
    assert document["schedules"]["nba_slate"] == "22:00"
    assert document["dry_run"] is True
    assert document["schedules"]["nba_late_slate"] == "00:30"
    assert document["markets"]["NBA"]["total_ft"]["min_prob"] == 0.5  # D78.
    assert document["markets"]["NCAA"]["handicap"]["min_prob"] == 0.5


def _changed(api: Api, **changes: Any) -> dict[str, Any]:
    current = api.get("/config").json()
    return {"version": current["version"], "document": {**current["document"], **changes}}


def test_a_change_is_saved_versioned_and_audited(api: Api) -> None:
    response = api.send("PUT", "/config", _changed(api, min_legs=3))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["document"]["min_legs"] == 3
    assert body["diff"] == [{"path": "min_legs", "from": 2, "to": 3}]
    assert body["changed_by"] == "dashboard"
    audit = api.table.get_item(Key={"pk": "CONFIG", "sk": "v#000001"})["Item"]
    assert audit["diff"][0]["path"] == "min_legs"
    second = api.send("PUT", "/config", _changed(api, min_legs=4))
    assert second.json()["version"] == 2


def test_a_stale_change_is_refused_rather_than_overwriting(api: Api) -> None:
    stale = _changed(api, min_legs=3)
    assert api.send("PUT", "/config", stale).status_code == 200
    again = {**stale, "document": {**stale["document"], "min_legs": 5}}
    assert api.send("PUT", "/config", again).status_code == 409
    assert api.get("/config").json()["document"]["min_legs"] == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"min_legs": 1},  # D4.
        {"min_minutes_before_tip": 5},  # Invariant 2.
        {"schedules": {"ncaa_slate": "15:00"}},
        {"schedules": {"ncaa_slate": "25:00", "nba_slate": "22:00", "nightly_learn": "11:00"}},
        {"unknown_field": True},
    ],
)
def test_settings_that_would_break_a_rule_are_refused(api: Api, changes: dict[str, Any]) -> None:
    response = api.send("PUT", "/config", _changed(api, **changes))
    assert response.status_code == 422
    assert api.get("/config").json()["version"] == 0
    assert api.schedules.updates == []  # Nothing moved on a refused change.


def test_only_changed_schedules_reach_the_scheduler(api: Api) -> None:
    current = api.get("/config").json()["document"]["schedules"]
    payload = _changed(api, schedules={**current, "nba_slate": "23:00"})
    assert api.send("PUT", "/config", payload).status_code == 200
    assert api.schedules.updates == [("nba_slate", "23:00")]


def test_a_threshold_cannot_be_set_outside_a_probability(api: Api) -> None:
    payload = _changed(api)
    payload["document"]["markets"]["NBA"]["handicap"]["min_prob"] = 1.5
    assert api.send("PUT", "/config", payload).status_code == 422


def test_a_gate_verdict_cannot_be_edited_through_settings(api: Api) -> None:
    payload = _changed(api)
    payload["document"]["markets"]["NBA"]["handicap"]["passed_gate"] = True
    assert api.send("PUT", "/config", payload).status_code == 422


def test_a_schedule_change_validates_and_reaches_the_scheduler(api: Api) -> None:
    response = api.send("PUT", "/schedules/nba_slate", {"time": "23:15"})
    assert response.status_code == 200, response.text
    assert response.json()["document"]["schedules"]["nba_slate"] == "23:15"
    assert ("nba_slate", "23:15") in api.schedules.updates


@pytest.mark.parametrize("time_text", ["25:00", "7:00", "noon", "12:60"])
def test_a_bad_time_is_refused(api: Api, time_text: str) -> None:
    assert api.send("PUT", "/schedules/nba_slate", {"time": time_text}).status_code == 422


def test_an_unknown_schedule_is_a_404(api: Api) -> None:
    assert api.send("PUT", "/schedules/nope", {"time": "10:00"}).status_code == 404


def test_changes_between_finds_nested_differences() -> None:
    assert changes_between({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}}) == [
        {"path": "a.c", "from": 2, "to": 3}
    ]
    assert changes_between({"a": 1}, {"a": 1}) == []


# Settings actually taking effect, because a dead toggle would be worse than none.


def test_saved_settings_reach_the_engine() -> None:
    document = default_settings()
    document["min_legs"] = 3
    document["min_minutes_before_tip"] = 45
    document["allow_alt_lines"] = True
    document["leagues"]["NBA"]["enabled"] = False
    document["markets"]["NCAA"]["total_1h"] = {"enabled": False, "min_prob": 0.6, "min_edge": 0.02}
    applied = apply_settings(config(), document)
    assert (applied.min_legs, applied.min_minutes_before_tip) == (3, 45)
    assert applied.allow_alt_lines is True
    assert applied.leagues["NBA"].enabled is False
    market = applied.markets["NCAA"]["total_1h"]
    assert (market.enabled, market.min_prob, market.min_edge) == (False, 0.6, 0.02)
    assert market.passed_gate is config().markets["NCAA"]["total_1h"].passed_gate


def test_no_saved_settings_leaves_the_engine_alone() -> None:
    assert apply_settings(config(), None) == config()


def test_the_pipeline_reads_what_the_api_wrote(api: Api) -> None:
    assert api.pipeline.settings() is None
    api.send("PUT", "/config", _changed(api, pause_all=True))
    saved = api.pipeline.settings()
    assert saved is not None
    assert saved["pause_all"] is True


def test_pause_all_stops_scheduled_jobs_and_a_new_time_moves_them(tmp_path: Path) -> None:
    now = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    assert run_due(tmp_path, now, lambda *_, **__: None, {"pause_all": True}) == []
    moved = {"nightly_learn": "12:30", "nba_slate": "23:00"}
    finished = {"grade": now.isoformat()}
    assert due_jobs(now, finished, moved) == []
    later = datetime(2026, 11, 7, 11, 30, tzinfo=UTC)  # 12:30 in Lagos.
    assert {job.name for job in due_jobs(later, {"grade": later.isoformat()}, moved)} == {
        "learn-NBA", "learn-NCAA",
    }  # fmt: skip


def test_the_system_page_shows_alerts_freshness_and_the_dry_run_state(api: Api) -> None:
    api.pipeline.alert(NOW, "CRITICAL", "run-failed", "It broke", "r1")
    folder = api.root / "ingest"
    folder.mkdir()
    (folder / "runs.jsonl").write_text('{"at": "old"}\n{"at": "new"}\n', "utf-8")
    body = api.get("/system").json()
    assert body["alerts"][0]["message"] == "It broke"
    assert body["freshness"]["last_ingest"] == "new"
    assert body["freshness"]["last_odds_fetch"] is None
    assert body["dry_run"] is True
    assert body["scheduled_dry_run"] is True
    assert body["cost"] is None
    assert body["budget_usd"] == [10, 25, 50]


def test_details_sit_behind_the_secret(api: Api) -> None:
    _run(api, "r1", NOW)
    body = api.get("/health/details").json()
    assert body["env"] == "local"
    assert body["champion_versions"] == {"NBA": None, "NCAA": None}
    assert body["last_runs"][0]["run_id"] == "r1"


# Labels.


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"family": "handicap", "line": -6.5, "side": "home"}, "Duke -6.5"),
        ({"family": "handicap", "line": -6.5, "side": "away"}, "UNC +6.5"),
        ({"family": "handicap", "line": 3.0, "side": "home"}, "Duke +3"),
        ({"family": "total_ft", "line": 145.5, "side": "over"}, "Over 145.5"),
        ({"family": "total_1h", "line": 66.5, "side": "under"}, "1H Under 66.5"),
        ({"family": "team_total", "line": 70.5, "side": "over", "team": "home"}, "Duke Over 70.5"),
        ({"family": "team_total", "line": 70.5, "side": "under", "team": "away"}, "UNC Under 70.5"),
    ],
)
def test_picks_read_the_way_a_person_would_say_them(changes: dict[str, Any], expected: str) -> None:
    assert pick_label(_leg(**changes), "Duke", "UNC") == expected


def test_a_leg_summary_is_what_the_today_page_needs() -> None:
    summary = leg_summary(_leg(), "Duke", "UNC")
    assert summary["matchup"] == "UNC at Duke"
    assert summary["tip_time"] == TIP.isoformat()


# Staying fast and in step.


def test_the_api_path_never_loads_heavy_libraries() -> None:
    # In its own process, because this test process has already imported them.
    script = """
import sys, boto3
from moto import mock_aws
from fastapi.testclient import TestClient
from hoopsedge.api.main import Deps, LocalRunStarter
from hoopsedge.api.app import create_app
from hoopsedge.api.store import ApiStore, Artifacts
from hoopsedge.common.dynamo import ensure_table
from pathlib import Path
with mock_aws():
    r = boto3.resource(
        "dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    ensure_table(r, "t")
    store = ApiStore(r.Table("t"))
    deps = Deps(store, Artifacts(Path("/nonexistent")), LocalRunStarter(), secret=lambda: "k")
    client = TestClient(create_app(deps))
    for path in ("/health/details", "/slates/2026-11-07", "/tickets/x", "/games/x", "/runs",
                 "/track-record", "/models/NBA", "/evidence/NBA", "/unmatched", "/teams/NCAA",
                 "/config", "/system"):
        client.get(path, headers={"x-hoopsedge-key": "k"})
heavy = sorted({"lightgbm", "polars", "sklearn", "scipy", "pyarrow"} & set(sys.modules))
print(heavy)
sys.exit(1 if heavy else 0)
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_read_stays_under_300_ms_at_the_95th_percentile(api: Api) -> None:
    api.game("g1")
    api.save("t1", _ticket([_leg(rank, "g1") for rank in range(1, 21)]))
    slowest: dict[str, float] = {}
    for path in (
        f"/slates/{SLATE}",
        "/tickets/t1",
        "/games/g1",
        "/runs",
        "/track-record",
        "/config",
    ):
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            assert api.get(path).status_code == 200
            samples.append(time.perf_counter() - started)
        slowest[path] = sorted(samples)[18]
    assert max(slowest.values()) < 0.3, slowest


def test_the_published_schema_matches_the_code(tmp_path: Path) -> None:
    # The dashboard's TypeScript types are generated from this file, so drift fails here.
    fresh = export_openapi(tmp_path / "openapi.json")
    assert fresh == json.loads(OPENAPI_FILE.read_text("utf-8")), (
        "Run: docker compose run --rm dev python -m hoopsedge.api.main export"
    )


def test_the_schema_covers_every_endpoint() -> None:
    deps = Deps(None, Artifacts(Path()), FakeStarter())
    paths = openapi_document(create_app(deps))["paths"]
    for path in (
        "/health", "/slates/{slate_date}", "/tickets/{ticket_id}", "/games/{game_id}", "/runs",
        "/runs/{run_id}", "/track-record", "/models/{league}", "/evidence/{league}", "/unmatched",
        "/unmatched/{event_id}/resolve", "/config", "/schedules/{name}",
        "/models/{league}/rollback", "/system",
    ):  # fmt: skip
        assert path in paths, path


# D78: the page must say what will really happen, not only what the Settings switch says.


def _turn_off_dry_run(api: Api) -> None:
    view = api.get("/config").json()
    document = {**view["document"], "dry_run": False}
    response = api.send("PUT", "/config", {"version": view["version"], "document": document})
    assert response.status_code == 200, response.text


def test_the_switch_alone_doesnt_make_codes_real_on_this_machine(api: Api) -> None:
    _turn_off_dry_run(api)
    body = api.get("/system").json()
    assert body["dry_run"] is True
    assert body["scheduled_dry_run"] is True


def test_with_local_booking_run_now_is_real_and_schedules_stay_practice(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOOPSEDGE_LOCAL_BOOKING", "dashboard")
    monkeypatch.setenv("DRY_RUN", "false")
    _turn_off_dry_run(api)
    body = api.get("/system").json()
    assert body["dry_run"] is False
    assert body["scheduled_dry_run"] is True
