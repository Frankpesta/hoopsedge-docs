"""The curated historical lines table (D27, D44)."""

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from hoopsedge.backtest.lines import CLOSE, CURRENT, build_lines, parse_odds
from hoopsedge.features.registry import FEATURES


def _price(decimal: float) -> dict[str, Any]:
    return {"decimal": decimal}


def _book(name: str = "DraftKings", priority: int = 1, **extra: Any) -> dict[str, Any]:
    item = {
        "provider": {"name": name, "priority": priority},
        "spread": 1.5,
        "overUnder": 227.5,
        "homeTeamOdds": {
            "close": {"pointSpread": {"american": "+2.5"}, "spread": _price(1.86)},
        },
        "awayTeamOdds": {
            "close": {"pointSpread": {"american": "-2.5"}, "spread": _price(1.95)},
        },
        "close": {"total": {"american": "228.5"}, "over": _price(1.9), "under": _price(1.91)},
    }
    item.update(extra)
    return item


def test_closing_line_is_preferred_and_keeps_its_sign_and_prices() -> None:
    line = parse_odds({"items": [_book()]})
    assert line is not None
    assert line.kind == CLOSE
    assert line.provider == "DraftKings"
    assert line.spread_home == 2.5  # A positive home spread: the home team is the underdog (D27).
    assert line.total == 228.5
    assert (line.price_home, line.price_away) == (1.86, 1.95)
    assert (line.price_over, line.price_under) == (1.9, 1.91)


def test_older_files_fall_back_to_current_and_say_so() -> None:
    line = parse_odds(
        {
            "items": [
                {
                    "provider": {"name": "ESPN BET", "priority": 1},
                    "spread": -4.0,
                    "overUnder": 210.0,
                    "homeTeamOdds": {},
                    "awayTeamOdds": {},
                }
            ]
        }
    )
    assert line is not None
    assert line.kind == CURRENT  # Timing unverified: D27 says it isn't a closing line.
    assert line.spread_home == -4.0
    assert line.total == 210.0
    assert line.price_home is None


def test_non_book_and_in_play_providers_are_ignored() -> None:
    items = [_book("consensus"), _book("numberfire"), _book("Bet365 - Live Odds")]
    assert parse_odds({"items": items}) is None


def test_highest_priority_book_wins_whatever_the_order() -> None:
    items = [_book("Second", 2), _book("First", 1)]
    first = parse_odds({"items": items})
    second = parse_odds({"items": list(reversed(items))})
    assert first is not None
    assert second is not None
    assert first.provider == second.provider == "First"


def test_pick_em_is_a_zero_spread() -> None:
    item = _book()
    item["homeTeamOdds"]["close"]["pointSpread"]["american"] = "EVEN"
    line = parse_odds({"items": [item]})
    assert line is not None
    assert line.spread_home == 0.0


def test_an_unreadable_close_falls_back_to_current_rather_than_guessing() -> None:
    item = _book()
    item["homeTeamOdds"]["close"]["pointSpread"]["american"] = "n/a"
    line = parse_odds({"items": [item]})
    assert line is not None
    assert line.kind == CURRENT
    assert line.spread_home == 1.5


def test_not_found_file_gives_no_line() -> None:
    assert parse_odds({"items": [], "not_found": True}) is None


def _write_game(data_dir: Path, league: str, day: date, game_id: str, odds: dict[str, Any]) -> None:
    folder = data_dir / f"raw/espn/{league}/{day:%Y/%m/%d}"
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / f"{game_id}.odds.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(odds, handle)


def _games(rows: list[tuple[str, date]], league: str = "NBA", season: int = 2026) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "league": [league] * len(rows),
            "season": [season] * len(rows),
            "game_id": [row[0] for row in rows],
            "slate_date_et": [row[1] for row in rows],
            "tip_utc": [
                datetime(row[1].year, row[1].month, row[1].day, 23, tzinfo=UTC) for row in rows
            ],
        }
    )


def test_build_writes_a_row_per_game_with_a_line_and_counts_the_rest(tmp_path: Path) -> None:
    day = date(2026, 1, 5)
    _write_game(tmp_path, "NBA", day, "1", {"items": [_book()]})
    _write_game(tmp_path, "NBA", day, "2", {"items": [], "not_found": True})
    result = build_lines(tmp_path, "NBA", _games([("1", day), ("2", day), ("3", day)]))
    frame = pl.read_parquet(tmp_path / "curated/lines/league=NBA/season=2026.parquet")
    assert frame["game_id"].to_list() == ["1"]
    assert result[2026] == {"games": 3, "lines": 1, "close": 1, "current": 0, "no_file": 1}


def test_a_corrupt_odds_file_fails_loudly(tmp_path: Path) -> None:
    day = date(2026, 1, 5)
    folder = tmp_path / f"raw/espn/NBA/{day:%Y/%m/%d}"
    folder.mkdir(parents=True)
    (folder / "1.odds.json.gz").write_bytes(b"not gzip")
    with pytest.raises(ValueError, match="1.odds"):
        build_lines(tmp_path, "NBA", _games([("1", day)]))


def test_market_lines_are_never_model_features() -> None:
    # Closing lines come after the run time, so as an input they'd leak (invariant 1).
    banned = ("spread", "total", "line", "odds", "price", "close")
    assert not [f for f in FEATURES if any(word in f for word in banned)]
