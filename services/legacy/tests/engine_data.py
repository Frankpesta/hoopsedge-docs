"""Builders for ticket-engine tests, and a normalizer for the recorded SportyBet slate."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from hoopsedge.engine import (
    FAMILIES,
    Confidence,
    EngineConfig,
    InjuryStatus,
    LeagueSettings,
    MarketSettings,
    Offer,
    Outcome,
    PeriodPrediction,
    Prediction,
)
from hoopsedge.engine.build import main_line
from hoopsedge.models.calibration import Calibrator

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TIP = NOW + timedelta(hours=6)
IDENTITY = Calibrator("platt", (1.0,), (0.0,), 1000)
SIDES = {"handicap": ("home", "away")}
FIXTURES = Path(__file__).parent / "fixtures"


def period(
    mu_home: float = 110.0, mu_away: float = 105.0, sd_margin: float = 12.0, sd_total: float = 17.0
) -> PeriodPrediction:
    return PeriodPrediction(
        mu_home=mu_home,
        mu_away=mu_away,
        sd_margin=sd_margin,
        sd_total=sd_total,
        sd_home=11.0,
        sd_away=11.0,
    )


def prediction(game_id: str = "g1", league: str = "NCAA", **changes: Any) -> Prediction:
    fields: dict[str, Any] = {
        "game_id": game_id,
        "league": league,
        "model_version": "20261106-1814-abc1234",
        "feature_hash": f"hash-{game_id}",
        "ft": period(),
        "h1": period(55.0, 52.0, 9.0, 11.0),
        "confidence": Confidence(
            games_home=20, games_away=20, prior_weight_home=0.1, prior_weight_away=0.1
        ),
        "injury": InjuryStatus(questionable_starter=False, snapshot_at=NOW),
    }
    return Prediction(**{**fields, **changes})


def offer(
    game_id: str | None = "g1",
    family: str = "total_ft",
    line: float = 200.5,
    odds: tuple[float, float] = (1.91, 1.91),
    league: str = "NCAA",
    team: str | None = None,
    **changes: Any,
) -> Offer:
    first, second = SIDES.get(family, ("over", "under"))
    key = "hcp" if family == "handicap" else "total"
    market_id = changes.pop(
        "market_id", {"handicap": "223", "total_ft": "225", "total_1h": "68"}.get(family)
    ) or ("227" if team == "home" else "228")
    fields: dict[str, Any] = {
        "league": league,
        "event_id": f"sr:match:{game_id or 'unknown'}",
        "game_id": game_id,
        "family": family,
        "market_id": market_id,
        "specifier": f"{key}={line}",
        "line": line,
        "team": team,
        "outcomes": (
            Outcome.model_validate({"side": first, "outcome_id": f"{first}-id", "odds": odds[0]}),
            Outcome.model_validate({"side": second, "outcome_id": f"{second}-id", "odds": odds[1]}),
        ),
        "tip_time": TIP,
        "status": "not_started",
        "bookable": True,
        "favourite": False,
        "fetched_at": NOW,
    }
    return Offer(**{**fields, **changes})


def settings(min_prob: float | None = 0.52, min_edge: float | None = 0.0) -> MarketSettings:
    return MarketSettings(
        enabled=True, passed_gate=True, edge_label="measured", min_prob=min_prob, min_edge=min_edge
    )


def config(**changes: Any) -> EngineConfig:
    fields: dict[str, Any] = {
        "leagues": {
            "NBA": LeagueSettings(enabled=True, max_legs_per_ticket=20, max_tickets_per_market=1),
            "NCAA": LeagueSettings(
                enabled=True, max_legs_per_ticket=20, max_tickets_per_market=5,
                max_legs_per_market=100,
            ),
        },
        "markets": {
            league: {family: settings() for family in FAMILIES} for league in ("NBA", "NCAA")
        },
        "calibrators": {
            league: {family: IDENTITY for family in FAMILIES} for league in ("NBA", "NCAA")
        },
    }  # fmt: skip
    return EngineConfig(**{**fields, **changes})


def _digest(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


def recorded_offers(now: datetime) -> list[Offer]:
    """Real SportyBet list responses, normalized by the rules in `sportybet/markets.yaml`.

    A stand-in until Phase 6's normalizer and matcher exist: the event ID doubles as the game ID.
    """
    ids = yaml.safe_load(
        (Path(__file__).parents[1] / "hoopsedge/sportybet/markets.yaml").read_text()
    )
    known: dict[str, tuple[str, str | None, dict[str, str]]] = {}
    for family, spec in ids["families"].items():
        for market in spec["markets"]:
            known[market["market_id"]] = (family, market.get("team"), spec["outcomes"])
    offers: list[Offer] = []
    pages = (
        ("NBA", "events-nba-page1"),
        ("NCAA", "events-ncaa-page1"),
        ("NCAA", "events-ncaa-page2"),
    )
    seen: set[str] = set()
    for league, name in pages:
        document = json.loads((FIXTURES / f"sportybet/{name}.json").read_text("utf-8"))
        for tournament in document["data"]["tournaments"]:
            for event in tournament["events"]:
                if event["eventId"] in seen:  # Real pages overlap (D70).
                    continue
                seen.add(event["eventId"])
                for market in event["markets"]:
                    if market["id"] not in known or "specifier" not in market:
                        continue
                    family, team, sides = known[market["id"]]
                    outcomes = []
                    for item in market["outcomes"]:
                        if item["id"] in sides and float(item.get("odds", 0)) > 1:
                            outcomes.append(
                                Outcome.model_validate(
                                    {
                                        "side": sides[item["id"]],
                                        "outcome_id": item["id"],
                                        "odds": float(item["odds"]),
                                    }
                                )
                            )
                    if len(outcomes) != 2:
                        continue
                    offers.append(
                        Offer.model_validate(
                            {
                                "league": league,
                                "event_id": event["eventId"],
                                "game_id": event["eventId"],
                                "family": family,
                                "market_id": market["id"],
                                "specifier": market["specifier"],
                                "line": float(market["specifier"].split("=")[1]),
                                "team": team,
                                "outcomes": (outcomes[0], outcomes[1]),
                                "tip_time": datetime.fromtimestamp(
                                    event["estimateStartTime"] / 1000, UTC
                                ),
                                "status": "not_started" if event["status"] == 0 else "live",
                                "bookable": market["status"] == ids["status"]["market_bookable"]
                                and all(item["isActive"] == 1 for item in market["outcomes"]),
                                "favourite": market.get("favourite") == 1,
                                "fetched_at": now,
                            }
                        )
                    )
    return offers


def recorded_predictions(offers: list[Offer], now: datetime) -> list[Prediction]:
    """Fixed predictions near the market, offset by each event's ID, so they never drift."""
    by_game: dict[str, dict[str, Offer]] = {}
    groups: dict[tuple[str, str], list[Offer]] = {}
    for item in offers:
        if item.team is None and item.game_id:  # Locked lines too, so every game gets a prediction.
            groups.setdefault((item.game_id, item.family), []).append(item)
    for (game_id, family), group in groups.items():
        by_game.setdefault(game_id, {})[family] = main_line(group)
    predictions: list[Prediction] = []
    for game_id, mains in sorted(by_game.items()):
        if "handicap" not in mains or "total_ft" not in mains:
            continue
        spin = _digest(game_id)
        margin = -mains["handicap"].line + ((spin % 13) - 6) * 0.5
        total = mains["total_ft"].line + ((spin // 13 % 17) - 8) * 0.5
        half = mains.get("total_1h")
        predictions.append(
            prediction(
                game_id,
                mains["handicap"].league,
                ft=period((total + margin) / 2, (total - margin) / 2),
                h1=None
                if half is None
                else period(half.line / 2 + 1, half.line / 2 - 1 + (spin % 5 - 2) * 0.5, 9.0, 11.0),
                injury=InjuryStatus(questionable_starter=False, snapshot_at=now),
            )
        )
    return predictions
