"""Curated historical market lines, from the raw ESPN core odds files (D27, D44).

Used only to score and calibrate models, never as a model input: a closing line comes after the
slate's run time. Spread is from the home team's side, so a negative spread means home is favoured.

Closing lines exist from 2023-24. Older files have only `current`, whose timing isn't verified,
so those rows are labelled and must not be described as closing lines.
"""

import gzip
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.ingest.curated import write_parquet
from hoopsedge.ingest.espn_coverage import is_book_line

CLOSE = "close"
CURRENT = "current"
PICK_EM = {"EVEN", "PK", "PICK"}
LAST_PRIORITY = 1_000_000

LINE_SCHEMA: dict[str, Any] = {
    "league": pl.String,
    "season": pl.Int64,
    "game_id": pl.String,
    "provider": pl.String,
    "kind": pl.String,
    "spread_home": pl.Float64,
    "total": pl.Float64,
    "price_home": pl.Float64,
    "price_away": pl.Float64,
    "price_over": pl.Float64,
    "price_under": pl.Float64,
}


@dataclass(frozen=True)
class MarketLine:
    provider: str
    kind: str
    spread_home: float
    total: float
    price_home: float | None = None
    price_away: float | None = None
    price_over: float | None = None
    price_under: float | None = None


def _number(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    if text.strip().upper() in PICK_EM:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _decimal(block: Any) -> float | None:
    value = block.get("decimal") if isinstance(block, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _closing(item: dict[str, Any], provider: str) -> MarketLine | None:
    home = (item.get("homeTeamOdds") or {}).get("close") or {}
    away = (item.get("awayTeamOdds") or {}).get("close") or {}
    close = item.get("close") or {}
    spread = _number((home.get("pointSpread") or {}).get("american"))
    total = _number((close.get("total") or {}).get("american"))
    if spread is None or total is None:
        return None
    return MarketLine(
        provider,
        CLOSE,
        spread,
        total,
        _decimal(home.get("spread")),
        _decimal(away.get("spread")),
        _decimal(close.get("over")),
        _decimal(close.get("under")),
    )


def parse_odds(document: dict[str, Any]) -> MarketLine | None:
    """The highest-priority book's line for one game, or None if no book priced it."""
    books = [item for item in document.get("items") or [] if is_book_line(item)]
    if not books:
        return None
    book = min(
        books,
        key=lambda item: (
            item["provider"].get("priority", LAST_PRIORITY),
            str(item["provider"].get("name")),
        ),
    )
    provider = str(book["provider"].get("name"))
    return _closing(book, provider) or MarketLine(
        provider, CURRENT, float(book["spread"]), float(book["overUnder"])
    )


def _read(path: Path) -> dict[str, Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            document: dict[str, Any] = json.load(handle)
    except (OSError, EOFError, json.JSONDecodeError) as error:
        raise ValueError(f"Unreadable odds file {path.name}: {error}") from error
    return document


def build_lines(data_dir: Path, league: str, games: pl.DataFrame) -> dict[int, dict[str, int]]:
    """Write one Parquet per season. A game with no file or no book simply has no row."""
    summary: dict[int, dict[str, int]] = {}
    for (season,), group in games.group_by(["season"], maintain_order=True):
        rows: list[dict[str, Any]] = []
        no_file = 0
        for row in group.to_dicts():
            day = row["slate_date_et"]
            path = data_dir / f"raw/espn/{league}/{day:%Y/%m/%d}/{row['game_id']}.odds.json.gz"
            if not path.exists():
                no_file += 1
                continue
            line = parse_odds(_read(path))
            if line is not None:
                rows.append(
                    {"league": league, "season": season, "game_id": row["game_id"], **asdict(line)}
                )
        frame = pl.DataFrame(rows, schema=LINE_SCHEMA)
        write_parquet(frame, data_dir / f"curated/lines/league={league}/season={season}.parquet")
        summary[int(str(season))] = {
            "games": len(group),
            "lines": len(frame),
            "close": frame.filter(pl.col("kind") == CLOSE).height,
            "current": frame.filter(pl.col("kind") == CURRENT).height,
            "no_file": no_file,
        }
    return summary
