"""SportyBet's lists to the engine's `Offer`s (docs/07 section 6.2, D33, D70).

An event of an unexpected shape is recorded and skipped, never guessed at. All IDs come from
`markets.yaml`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from hoopsedge.engine.schemas import NOT_STARTED, Offer
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.sportybet.client import MARKETS, SportyBetClient, SportyBetError
from hoopsedge.teams.matcher import SWAPPED

SLATE_WINDOW = timedelta(hours=6)
FLIPPED = {"home": "away", "away": "home"}


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RawOutcome(_Raw):
    id: str
    odds: str | None = None
    isActive: int


class RawMarket(_Raw):
    id: str
    specifier: str
    status: int
    outcomes: list[RawOutcome]
    favourite: int = 0


class RawEvent(_Raw):
    eventId: str
    homeTeamName: str
    awayTeamName: str
    estimateStartTime: int
    status: int
    markets: list[RawMarket]


@dataclass(frozen=True)
class _MarketSpec:
    family: str
    team: str | None
    specifier_key: str
    outcomes: dict[str, str]


def _catalog() -> dict[str, _MarketSpec]:
    return {
        str(market["market_id"]): _MarketSpec(
            name, market.get("team"), spec["specifier_key"], spec["outcomes"]
        )
        for name, spec in MARKETS["families"].items()
        for market in spec["markets"]
    }


CATALOG = _catalog()


@dataclass(frozen=True)
class SportyEvent:
    league: str
    raw: RawEvent
    start: datetime

    @property
    def event_id(self) -> str:
        return self.raw.eventId

    @property
    def status(self) -> str:
        return NOT_STARTED if self.raw.status == MARKETS["status"]["event_not_started"] else "live"


@dataclass(frozen=True)
class OfferSnapshot:
    league: str
    fetched_at: datetime
    events: tuple[SportyEvent, ...]
    failures: tuple[tuple[str, str], ...]  # Event ID and what was wrong with it.


def _odds(outcome: RawOutcome) -> float | None:
    try:
        return float(outcome.odds) if outcome.odds is not None else None
    except ValueError:
        return None


def _is_bookable(market: RawMarket) -> bool:
    return (
        market.status == MARKETS["status"]["market_bookable"]
        and len(market.outcomes) == 2
        and all(item.isActive == 1 for item in market.outcomes)
    )


def _check_market(market: RawMarket, spec: _MarketSpec) -> None:
    key, _, value = market.specifier.partition("=")
    if key != spec.specifier_key:
        raise ValueError(f"market {market.id} has specifier {market.specifier!r}")
    float(value)
    if {item.id for item in market.outcomes} != set(spec.outcomes):
        raise ValueError(f"market {market.id} has unknown outcomes")
    if _is_bookable(market) and any((_odds(item) or 0.0) <= 1.0 for item in market.outcomes):
        raise ValueError(f"market {market.id} is open with unusable odds")


def parse_event(league: str, document: dict[str, Any]) -> SportyEvent:
    """Raises ValueError or ValidationError if anything we rely on has changed shape."""
    markets = [item for item in document.get("markets") or [] if str(item.get("id")) in CATALOG]
    raw = RawEvent.model_validate({**document, "markets": markets})
    for market in raw.markets:
        _check_market(market, CATALOG[market.id])
    return SportyEvent(league, raw, datetime.fromtimestamp(raw.estimateStartTime / 1000, UTC))


def fetch_snapshot(client: SportyBetClient, league: str, now: datetime) -> OfferSnapshot:
    """Every event in the league's tournaments, each once (D70: real pages overlap)."""
    seen: dict[str, dict[str, Any]] = {}
    for tournament_id in MARKETS["leagues"][league]["tournament_ids"]:
        page = 1
        before = len(seen)
        total = 0
        while True:
            document = client.events_page(tournament_id, page)
            data = document.get("data") or {}
            total = int(data.get("totalNum") or 0)
            events = [
                event
                for group in data.get("tournaments") or []
                for event in group.get("events", [])
            ]
            known = len(seen)
            for event in events:
                seen.setdefault(str(event.get("eventId")), event)
            # A page with nothing new ends the loop, so a repeating API can't hold it forever.
            if len(seen) == known or len(seen) - before >= total:
                break
            page += 1
        if len(seen) - before < total:
            raise SportyBetError(
                f"{league} {tournament_id}: got {len(seen) - before} events of {total}"
            )
    parsed: list[SportyEvent] = []
    failures: list[tuple[str, str]] = []
    for event_id, event in sorted(seen.items()):
        try:
            parsed.append(parse_event(league, event))
        except (ValidationError, ValueError) as error:
            failures.append((event_id, str(error)[:300]))
    return OfferSnapshot(league, now, tuple(parsed), tuple(failures))


def slate_bounds(slate: date) -> tuple[datetime, datetime]:
    """The slate's Eastern date, 6 hours either side, because SportyBet's times can differ."""
    start = datetime(slate.year, slate.month, slate.day, tzinfo=EASTERN)
    return start - SLATE_WINDOW, start + timedelta(days=1) + SLATE_WINDOW


def normalize_event(
    event: SportyEvent, game_id: str | None, orientation: str | None, fetched_at: datetime
) -> list[Offer]:
    """One `Offer` per line. `home` always means the canonical home team.

    When SportyBet lists a neutral game the other way round, the line and sides flip, but the IDs
    and the specifier stay as SportyBet has them, because the booking must use those.
    """
    swapped = orientation == SWAPPED
    offers: list[Offer] = []
    for market in event.raw.markets:
        spec = CATALOG[market.id]
        odds = {item.id: _odds(item) for item in market.outcomes}
        if any(value is None or value <= 1.0 for value in odds.values()):
            continue  # Only a locked line can get here: `parse_event` refuses an open one.
        line = float(market.specifier.partition("=")[2])
        outcomes = []
        for item in market.outcomes:
            side = spec.outcomes[item.id]
            outcomes.append(
                {
                    "side": FLIPPED[side] if swapped and side in FLIPPED else side,
                    "outcome_id": item.id,
                    "odds": odds[item.id],
                }
            )
        team = spec.team
        offers.append(
            Offer.model_validate(
                {
                    "league": event.league,
                    "event_id": event.event_id,
                    "game_id": game_id,
                    "family": spec.family,
                    "market_id": market.id,
                    "specifier": market.specifier,
                    "line": -line if swapped and spec.family == "handicap" else line,
                    "team": FLIPPED[team] if swapped and team else team,
                    "outcomes": tuple(outcomes),
                    "tip_time": event.start,
                    "status": event.status,
                    "bookable": _is_bookable(market),
                    "favourite": market.favourite == 1,
                    "fetched_at": fetched_at,
                }
            )
        )
    return offers
