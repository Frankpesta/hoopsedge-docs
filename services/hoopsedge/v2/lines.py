"""SportyBet event detail to line ladders, and which line to lean on and to book (brief §5.2, §6).

Market IDs come from `sportybet/markets.yaml`. Over and Under are told apart by each outcome's
`desc` ("Over 45.5"), never by hard-coded outcome IDs, and the number in `desc` must equal the
specifier's. A line is usable only when its market is open (`status` 0), it has exactly those two
outcomes, both active, both priced above 1.0. A market of a shape we don't expect stops the
event, not the run: the caller records why and skips it (invariant 7).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hoopsedge.sportybet.client import MARKETS

Direction = Literal["Over", "Under"]
NOT_STARTED = "Not start"


def _market_ids() -> tuple[str, str, str]:
    families = MARKETS["families"]
    total = str(families["total_ft"]["markets"][0]["market_id"])
    teams = {m["team"]: str(m["market_id"]) for m in families["team_total"]["markets"]}
    return total, teams["home"], teams["away"]


TOTAL_ID, HOME_TOTAL_ID, AWAY_TOTAL_ID = _market_ids()


class LineShapeError(ValueError):
    """A market we rely on has changed shape. The event is skipped and the reason kept."""


@dataclass(frozen=True)
class Line:
    market_id: str
    specifier: str
    line: float
    over_id: str
    under_id: str
    over_odds: float
    under_odds: float

    def outcome(self, direction: Direction) -> tuple[str, float]:
        if direction == "Over":
            return self.over_id, self.over_odds
        return self.under_id, self.under_odds


@dataclass(frozen=True)
class EventLines:
    event_id: str
    home_name: str
    away_name: str
    start: datetime
    match_status: str
    status: int
    totals: tuple[Line, ...]  # Ascending by line.
    home_totals: tuple[Line, ...]  # SportyBet's home team.
    away_totals: tuple[Line, ...]

    def not_started(self, now: datetime) -> bool:
        """Brief invariant 2: SportyBet still says "Not start", and the start is ahead."""
        return self.match_status == NOT_STARTED and self.status == 0 and self.start > now


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError as error:
        raise LineShapeError(f"not a number: {text!r}") from error


def parse_line(market: dict[str, Any]) -> Line | None:
    """None for a line that can't be booked now; LineShapeError for one we can't read."""
    market_id = str(market.get("id"))
    specifier = str(market.get("specifier") or "")
    key, _, value = specifier.partition("=")
    if key != "total":
        raise LineShapeError(f"market {market_id} has specifier {specifier!r}")
    line = _number(value)
    outcomes = market.get("outcomes") or []
    if len(outcomes) != 2:
        raise LineShapeError(f"market {market_id} {specifier} has {len(outcomes)} outcomes")
    sides: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        word, _, number = str(outcome.get("desc") or "").partition(" ")
        if word not in ("Over", "Under") or word in sides:
            raise LineShapeError(f"market {market_id} {specifier}: outcome {outcome.get('desc')!r}")
        if _number(number) != line:
            raise LineShapeError(f"market {market_id} {specifier}: desc {outcome.get('desc')!r}")
        sides[word] = outcome
    if market.get("status") != 0 or any(o.get("isActive") != 1 for o in outcomes):
        return None
    odds = {word: _number(str(item.get("odds"))) for word, item in sides.items()}
    if any(value <= 1.0 for value in odds.values()):
        raise LineShapeError(f"market {market_id} {specifier} is open with odds {odds}")
    return Line(
        market_id=market_id,
        specifier=specifier,
        line=line,
        over_id=str(sides["Over"]["id"]),
        under_id=str(sides["Under"]["id"]),
        over_odds=odds["Over"],
        under_odds=odds["Under"],
    )


def parse_event(data: dict[str, Any]) -> EventLines:
    ladders: dict[str, list[Line]] = {TOTAL_ID: [], HOME_TOTAL_ID: [], AWAY_TOTAL_ID: []}
    seen: set[tuple[str, str]] = set()
    for market in data.get("markets") or []:
        market_id = str(market.get("id"))
        if market_id not in ladders:
            continue
        key = (market_id, str(market.get("specifier")))
        if key in seen:
            raise LineShapeError(f"market {market_id} {key[1]} appears twice")
        seen.add(key)
        line = parse_line(market)
        if line is not None:
            ladders[market_id].append(line)
    return EventLines(
        event_id=str(data["eventId"]),
        home_name=str(data["homeTeamName"]),
        away_name=str(data["awayTeamName"]),
        start=datetime.fromtimestamp(int(data["estimateStartTime"]) / 1000, UTC),
        match_status=str(data.get("matchStatus") or ""),
        status=int(data.get("status", -1)),
        totals=tuple(sorted(ladders[TOTAL_ID], key=lambda item: item.line)),
        home_totals=tuple(sorted(ladders[HOME_TOTAL_ID], key=lambda item: item.line)),
        away_totals=tuple(sorted(ladders[AWAY_TOTAL_ID], key=lambda item: item.line)),
    )


def main_line(ladder: tuple[Line, ...]) -> Line | None:
    """The line whose Over and Under prices are closest (brief §6). Ties go to the line nearer
    the ladder's middle, then the lower total (audit item 16)."""
    if not ladder:
        return None
    middle = (ladder[0].line + ladder[-1].line) / 2
    return min(
        ladder,
        key=lambda item: (
            round(abs(item.over_odds - item.under_odds), 6),
            abs(item.line - middle),
            item.line,
        ),
    )


def booked_line(
    ladder: tuple[Line, ...],
    mode: str,
    direction: Direction,
    projection: float,
    min_cushion: float,
) -> Line | None:
    """Brief §6. `safest` books the lowest line for an Over and the highest for an Under.
    `safest_with_cushion` books that line only if the projection clears it by `min_cushion`;
    the safest line has the largest cushion, so if it fails, every line does."""
    if not ladder:
        return None
    if mode == "main":
        return main_line(ladder)
    chosen = ladder[0] if direction == "Over" else ladder[-1]
    if mode == "safest":
        return chosen
    if mode == "safest_with_cushion":
        cushion = projection - chosen.line if direction == "Over" else chosen.line - projection
        return chosen if cushion >= min_cushion - 1e-9 else None
    raise ValueError(f"Unknown line mode {mode!r}")
