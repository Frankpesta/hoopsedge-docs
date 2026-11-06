"""Which seasons the project keeps, as the owner decided (D39 and D57)."""

from datetime import date

from hoopsedge.ingest.backfill import FIRST_SEASON, seasons_for


def test_nba_starts_at_2018_19_and_ncaa_at_2025_26() -> None:
    assert FIRST_SEASON == {"NBA": 2019, "NCAA": 2026}


def test_ncaa_is_this_season_and_last() -> None:
    assert seasons_for("NCAA", date(2026, 11, 5)) == [2027, 2026]
