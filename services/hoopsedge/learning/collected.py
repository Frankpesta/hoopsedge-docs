"""Score the SportyBet lines the collector saved, for games the champion hadn't seen (D72).

Locally nothing runs a slate every day, but the collector does save real lines every 30 minutes.
Pairing those with what the champion would have said gives honest real-line rows for calibrators.
A game only counts if it tipped after the champion's training cutoff, and each uses the last line
saved before its tip, with features as of the slate's run time, so nothing later can leak in.
"""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.engine import Offer, Prediction, score_offers
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.learning.data import finals_frame, has_log, write_log
from hoopsedge.pipeline.predict import predict_games
from hoopsedge.ratings.history import GameRef
from hoopsedge.sportybet.offers import SportyEvent, normalize_event, parse_event
from hoopsedge.teams.matcher import CanonicalGame, TeamIndex, match_event

COLLECTED = "collected"
LAGOS_RUN_LABEL = "slate run time"


def _snapshots(
    data_dir: Path, league: str, slate: date
) -> Iterator[tuple[datetime, dict[str, Any]]]:
    """Each complete snapshot that could hold a line for this slate, oldest first."""
    for offset in (-1, 0, 1):
        day = slate + timedelta(days=offset)
        folder = data_dir / f"raw/sportybet/collector-{day:%Y%m%d}"
        for file in sorted(folder.glob(f"offers-{league}-*.json.gz")):
            with gzip.open(file, "rt", encoding="utf-8") as handle:
                document = json.load(handle)
            if document.get("complete"):
                yield datetime.fromisoformat(document["captured_at"]), document


def last_lines(data_dir: Path, league: str, slate: date) -> dict[str, tuple[datetime, SportyEvent]]:
    """Each event's last saved state before its own tip."""
    latest: dict[str, tuple[datetime, SportyEvent]] = {}
    for captured, document in _snapshots(data_dir, league, slate):
        for response in document["responses"]:
            data = response.get("data") or {}
            for group in data.get("tournaments") or []:
                for raw in group.get("events", []):
                    try:
                        event = parse_event(league, raw)
                    except ValueError:
                        continue  # The collector saves everything, and a bad event isn't scored.
                    if event.start.astimezone(EASTERN).date() == slate and captured < event.start:
                        latest[event.event_id] = (captured, event)
    return latest


def _game(row: dict[str, Any]) -> CanonicalGame:
    return CanonicalGame(
        game_id=row["game_id"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        tip_utc=row["tip_utc"],
        neutral=row["neutral"],
        slate_date_et=row["slate_date_et"],
    )


def _ref(league: str, row: dict[str, Any]) -> GameRef:
    return GameRef(
        league=league,
        season=row["season"],
        game_id=row["game_id"],
        tip_utc=row["tip_utc"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        neutral=row["neutral"],
        postseason=row["postseason"],
        slate_date_et=row["slate_date_et"],
    )


def offers_for_slate(
    lines: dict[str, tuple[datetime, SportyEvent]],
    games: list[CanonicalGame],
    index: TeamIndex,
) -> list[Offer]:
    offers: list[Offer] = []
    for captured, event in lines.values():
        match = match_event(
            event.raw.homeTeamName, event.raw.awayTeamName, event.start, games, index
        )
        if match.game_id is not None:
            offers += normalize_event(event, match.game_id, match.orientation, captured)
    return offers


def score_slate(
    data_dir: Path,
    league: str,
    slate: date,
    cutoff: datetime,
    model_folder: Path,
    run_time: datetime,
    index: TeamIndex,
    today: date,
) -> dict[str, Any]:
    """Writes one log for a finished slate, and skips any slate already done."""
    if has_log(data_dir, league, slate, COLLECTED):
        return {"slate": slate.isoformat(), "skipped": "already scored"}
    finals = finals_frame(data_dir, league, today).filter(
        (pl.col("slate_date_et") == slate) & (pl.col("tip_utc") > cutoff)
    )
    if not len(finals):
        return {"slate": slate.isoformat(), "skipped": "no finals after the model's cutoff"}
    rows = finals.to_dicts()
    offers = offers_for_slate(
        last_lines(data_dir, league, slate), [_game(row) for row in rows], index
    )
    if not offers:
        return {"slate": slate.isoformat(), "skipped": "no saved lines matched a game"}
    matched = {offer.game_id for offer in offers}
    refs = [_ref(league, row) for row in rows if row["game_id"] in matched]
    predictions: list[Prediction] = predict_games(data_dir, league, refs, run_time, model_folder)
    scored = score_offers(predictions, offers)
    write_log(data_dir, league, slate, COLLECTED, predictions, scored)
    return {"slate": slate.isoformat(), "games": len(refs), "lines": len(scored)}


def slates_to_score(cutoff: datetime, now: datetime) -> list[date]:
    """From the champion's cutoff up to yesterday, Eastern, whose games have all finished."""
    first = cutoff.astimezone(EASTERN).date()
    last = now.astimezone(UTC).astimezone(EASTERN).date() - timedelta(days=1)
    days = (last - first).days
    return [first + timedelta(days=number) for number in range(days + 1)]
