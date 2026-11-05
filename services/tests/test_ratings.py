"""Ratings math: recovery of known strengths, priors, home edge, Elo and first-half stats."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from hoopsedge.ratings.engine import Engine, load_config
from hoopsedge.ratings.history import History
from tests.synthetic import HOME_EDGE, empty_injuries, empty_players, season_frames

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def engine_for(
    games: pl.DataFrame,
    box: pl.DataFrame,
    league: str = "NBA",
    d1: dict[int, set[str]] | None = None,
) -> Engine:
    return Engine(History(league, games, box, empty_players(), empty_injuries(), d1 or {}))


def test_ratings_recover_true_strength() -> None:
    strengths = np.array([-12.0, -8.0, -4.0, -2.0, 0.0, 1.0, 3.0, 5.0, 8.0, 10.0, 12.0, -13.0])
    games, box = season_frames(2025, days=150, seed=11, strengths=strengths)
    snap = engine_for(games, box).ratings_as_of(2025, FUTURE)
    fitted = np.array(
        [snap.value("off", f"T{i:02d}") + snap.value("def", f"T{i:02d}") for i in range(12)]
    )
    assert np.corrcoef(strengths, fitted)[0, 1] > 0.97


def test_league_level_and_home_edge_are_recovered() -> None:
    games, box = season_frames(2025, days=200, seed=12)
    snap = engine_for(games, box).ratings_as_of(2025, FUTURE)
    assert snap.league["mu"] == pytest.approx(109, abs=4)
    assert snap.league["tau"] == pytest.approx(98, abs=2)
    assert snap.league["hca"] == pytest.approx(HOME_EDGE, abs=2.0)


def test_home_edge_is_measured_once_there_are_plenty_of_games() -> None:
    games, box = season_frames(2025, days=300, seed=12, league="NCAA")
    snap = engine_for(games, box, "NCAA").season_final(2025)
    assert snap.league["hca"] == pytest.approx(HOME_EDGE, abs=1.0)


def test_early_blowouts_by_home_favourites_do_not_inflate_the_home_edge() -> None:
    """Good teams host weak ones in the first days of a college season (D55)."""
    strengths = np.array([20.0] * 40 + [-20.0] * 40)
    games, box = season_frames(
        2027, teams=80, days=3, games_per_day=40, seed=13, league="NCAA", strengths=strengths
    )
    strong = {f"T{i:02d}" for i in range(40)}
    weak = {f"T{i:02d}" for i in range(40, 80)}
    mismatch = games.filter(pl.col("home_id").is_in(strong) & pl.col("away_id").is_in(weak))
    assert len(mismatch) > 10
    snap = engine_for(
        mismatch, box.filter(pl.col("game_id").is_in(mismatch["game_id"].to_list())), "NCAA"
    ).ratings_as_of(2027, FUTURE)
    assert snap.league["hca"] < 15


def test_a_team_with_no_games_sits_on_its_prior() -> None:
    games, box = season_frames(2025, teams=8, days=5, seed=13)
    snap = engine_for(games, box).ratings_as_of(2025, FUTURE)
    assert snap.value("off", "NEVER") == 0.0
    assert snap.games_played("NEVER") == 0
    assert snap.prior_weight("NEVER") == 1.0


def test_prior_weight_falls_as_games_accumulate() -> None:
    games, box = season_frames(2025, teams=6, days=30, seed=14)
    engine = engine_for(games, box)
    config = load_config()["NBA"]
    tips = games.sort("tip_utc")["tip_utc"].to_list()
    early = engine.ratings_as_of(2025, tips[10])
    late = engine.ratings_as_of(2025, FUTURE)
    team = "T00"
    assert late.prior_weight(team) < early.prior_weight(team)
    games_played = late.games_played(team)
    assert late.prior_weight(team) == pytest.approx(config["k"] / (config["k"] + games_played))


def test_prior_pulls_early_ratings_toward_last_season() -> None:
    strengths = np.array([15.0, -15.0, 5.0, -5.0, 0.0, 2.0, -2.0, 7.0, -7.0, 1.0, -1.0, 3.0])
    old_games, old_box = season_frames(2024, days=120, seed=15, strengths=strengths)
    new_games, new_box = season_frames(2025, days=120, seed=16, strengths=strengths[::-1])
    games = pl.concat([old_games, new_games])
    box = pl.concat([old_box, new_box])
    engine = engine_for(games, box)
    opening = datetime(2024, 11, 1, 12, 0, tzinfo=UTC)
    snap = engine.ratings_as_of(2025, opening)
    assert snap.games_visible == 0
    assert snap.value("off", "T00") + snap.value("def", "T00") > 3  # Was very good last season.
    assert snap.value("off", "T01") + snap.value("def", "T01") < -3
    end_last = engine.ratings_as_of(2024, FUTURE)
    regress = load_config()["NBA"]["r"]
    assert snap.value("elo", "T00") == pytest.approx(
        1500 + (1 - regress) * (end_last.value("elo", "T00") - 1500)
    )


def test_a_team_new_this_season_starts_near_the_bottom() -> None:
    old_games, old_box = season_frames(2024, teams=12, days=80, seed=17)
    new_games, new_box = season_frames(2025, teams=13, days=40, seed=18)
    games = pl.concat([old_games, new_games])
    box = pl.concat([old_box, new_box])
    snap = engine_for(games, box).ratings_as_of(2025, datetime(2024, 11, 1, tzinfo=UTC))
    previous = [snap.value("off", f"T{i:02d}") + snap.value("def", f"T{i:02d}") for i in range(12)]
    newcomer = snap.value("off", "T12") + snap.value("def", "T12")
    assert newcomer < float(np.median(previous))


def test_non_d1_teams_share_one_rating_in_ncaa_only() -> None:
    games, box = season_frames(2025, teams=8, days=40, seed=19, league="NCAA")
    d1 = {2025: {f"T{i:02d}" for i in range(6)}}
    engine = engine_for(games, box, "NCAA", d1)
    snap = engine.ratings_as_of(2025, FUTURE)
    assert engine.history.pooled_team(2025, "T06") == "NON_D1"
    assert engine.history.pooled_team(2025, "T07") == "NON_D1"
    assert engine.history.pooled_team(2025, "T00") == "T00"
    assert "NON_D1" in snap.team_ids
    assert "T06" not in snap.team_ids
    assert snap.value("off", "T00") != snap.value("off", "T01")
    nba = engine_for(*season_frames(2025, teams=8, days=20, seed=19)).ratings_as_of(2025, FUTURE)
    assert nba.value("off", "T06") != nba.value("off", "T07")


def test_games_without_a_box_still_move_elo_but_not_efficiency() -> None:
    games, box = season_frames(2025, teams=6, days=20, seed=20)
    first = games.sort("tip_utc").to_dicts()[0]
    no_box = box.filter(pl.col("game_id") != first["game_id"])
    flagged = games.with_columns((pl.col("game_id") == first["game_id"]).alias("box_missing"))
    with_box = engine_for(games, box).ratings_as_of(2025, FUTURE)
    without = engine_for(flagged, no_box).ratings_as_of(2025, FUTURE)
    assert with_box.value("elo", first["home_id"]) == without.value("elo", first["home_id"])
    assert with_box.value("off", first["home_id"]) != without.value("off", first["home_id"])
    assert with_box.games_played(first["home_id"]) == without.games_played(first["home_id"])


def one_game() -> tuple[pl.DataFrame, pl.DataFrame]:
    return season_frames(2025, teams=2, days=1, seed=21)


def test_elo_winner_gains_and_the_sum_is_kept() -> None:
    games, box = one_game()
    row = games.to_dicts()[0]
    snap = engine_for(games, box).ratings_as_of(2025, FUTURE)
    home, away = snap.value("elo", row["home_id"]), snap.value("elo", row["away_id"])
    assert home + away == pytest.approx(3000)
    assert (home > 1500) == (row["home_pts"] > row["away_pts"])


def test_elo_rewards_a_bigger_margin() -> None:
    games, box = one_game()
    row = games.to_dicts()[0]

    def set_margin(margin: int) -> pl.DataFrame:
        return games.with_columns(
            pl.lit(100 + margin).alias("home_pts"), pl.lit(100).alias("away_pts")
        )

    small = engine_for(set_margin(2), box).ratings_as_of(2025, FUTURE)
    large = engine_for(set_margin(30), box).ratings_as_of(2025, FUTURE)
    assert large.value("elo", row["home_id"]) > small.value("elo", row["home_id"]) > 1500


def test_first_half_efficiency_is_not_a_copy_of_full_game() -> None:
    """Docs/04's per-team points-share estimate would make the two identical (D51)."""
    games, box = season_frames(2025, days=60, seed=22)
    snap = engine_for(games, box).ratings_as_of(2025, FUTURE)
    teams = [f"T{i:02d}" for i in range(12)]
    full = [snap.value("off", team) for team in teams]
    half = [snap.value("off_1h", team) for team in teams]
    assert not np.allclose(full, half, atol=0.01)
    assert snap.league["tau_1h"] == pytest.approx(snap.league["tau"] / 2, rel=0.15)


def test_overtime_games_scale_tempo_back_to_regulation() -> None:
    games, box = season_frames(2025, teams=4, days=40, seed=23)
    plain = engine_for(games, box).ratings_as_of(2025, FUTURE).league["tau"]
    longer = box.with_columns((pl.col("poss") * 53 / 48).alias("poss"), pl.lit(53).alias("minutes"))
    assert engine_for(games, longer).ratings_as_of(2025, FUTURE).league["tau"] == pytest.approx(
        plain, abs=0.5
    )


def test_recent_games_count_more() -> None:
    days = 80
    games, box = season_frames(2025, teams=6, days=days, seed=24)
    as_of = datetime(2024, 11, 1, tzinfo=UTC) + timedelta(days=days + 1)
    first = games.sort("tip_utc").to_dicts()[0]
    last = games.sort("tip_utc").to_dicts()[-1]

    def bump(game_id: str) -> pl.DataFrame:
        return box.with_columns(
            pl.when((pl.col("game_id") == game_id) & pl.col("is_home"))
            .then(pl.col("pts") + 40)
            .otherwise(pl.col("pts"))
            .alias("pts")
        )

    base = engine_for(games, box).ratings_as_of(2025, as_of)
    old = engine_for(games, bump(first["game_id"])).ratings_as_of(2025, as_of)
    new = engine_for(games, bump(last["game_id"])).ratings_as_of(2025, as_of)
    old_shift = abs(old.value("off", first["home_id"]) - base.value("off", first["home_id"]))
    new_shift = abs(new.value("off", last["home_id"]) - base.value("off", last["home_id"]))
    assert new_shift > old_shift


def test_time_passing_with_no_new_results_changes_nothing() -> None:
    """End-of-season ratings use a far-future as_of, which must not fade every game (D52)."""
    games, box = season_frames(2025, days=60, seed=27)
    last_tip: datetime = games["tip_utc"].to_list()[-1]
    engine = engine_for(games, box)
    soon = engine.ratings_as_of(2025, last_tip + timedelta(hours=4))
    far = engine_for(games, box).ratings_as_of(2025, FUTURE)
    assert soon.values["off"].tobytes() == far.values["off"].tobytes()
    spread = max(far.values["off"]) - min(far.values["off"])
    assert spread > 1.0  # Real ratings, not priors with every game faded away.


def test_season_final_keeps_a_team_that_stopped_playing_early() -> None:
    """A team out of the playoffs mustn't fade back to its prior before next season (D52)."""
    games, box = season_frames(2025, teams=12, days=120, seed=28)
    late = date(2025, 6, 15)
    tail_games, tail_box = season_frames(2025, teams=2, days=1, seed=29, start=late)
    tail_games = tail_games.with_columns((pl.col("game_id") + "x").alias("game_id"))
    tail_box = tail_box.with_columns((pl.col("game_id") + "x").alias("game_id"))
    every = pl.concat([games, tail_games])
    boxes = pl.concat([box, tail_box])
    engine = engine_for(every, boxes)
    faded = engine.ratings_as_of(2025, FUTURE)
    final = engine.season_final(2025)
    team = "T07"  # Plays only in the regular season.
    assert abs(final.value("off", team) + final.value("def", team)) > 0.5
    assert abs(final.value("off", team) + final.value("def", team)) > abs(
        faded.value("off", team) + faded.value("def", team)
    )


def test_next_season_priors_come_from_the_full_season_rating() -> None:
    old_games, old_box = season_frames(2024, days=100, seed=30)
    new_games, new_box = season_frames(2025, days=10, seed=31)
    engine = engine_for(pl.concat([old_games, new_games]), pl.concat([old_box, new_box]))
    keep = 1 - load_config()["NBA"]["r"]
    prior = engine.priors_for(2025)
    assert prior.value("off", "T03") == pytest.approx(
        keep * engine.season_final(2024).value("off", "T03")
    )


def test_results_are_repeatable() -> None:
    games, box = season_frames(2025, days=30, seed=25)
    one = engine_for(games, box).ratings_as_of(2025, FUTURE)
    two = engine_for(games, box).ratings_as_of(2025, FUTURE)
    assert one.values["off"].tobytes() == two.values["off"].tobytes()


def test_a_full_snapshot_is_fast_enough_for_the_season_budget() -> None:
    """Docs/04: under 2 minutes per season, one snapshot per slate, about 165 NCAA slates."""
    import time

    teams = 360
    games, box = season_frames(
        2025, teams=teams, days=150, games_per_day=40, seed=26, league="NCAA"
    )
    engine = engine_for(games, box, "NCAA")
    started = time.perf_counter()
    for offset in (50, 100, 149):
        engine.ratings_as_of(2025, datetime(2024, 11, 1, tzinfo=UTC) + timedelta(days=offset))
    assert (time.perf_counter() - started) / 3 * 165 < 120
