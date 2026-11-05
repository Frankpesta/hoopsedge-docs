"""Feature vector, hashing, availability, the shared code path and the training builder."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from hoopsedge.features.builder import features_for_game, hash_features
from hoopsedge.features.registry import FEATURE_SET_VERSION, FEATURES, UNTRAINED_FEATURES
from hoopsedge.features.training import build_training_set, slate_run_time
from hoopsedge.ingest.curated import write_parquet
from hoopsedge.ratings.engine import Engine
from hoopsedge.ratings.history import GameRef, History
from tests.synthetic import PLAYER_SCHEMA, empty_injuries, empty_players, season_frames


def engine_for(
    games: pl.DataFrame,
    box: pl.DataFrame,
    players: pl.DataFrame | None = None,
    injuries: pl.DataFrame | None = None,
    league: str = "NBA",
) -> Engine:
    return Engine(
        History(
            league,
            games,
            box,
            players if players is not None else empty_players(),
            injuries if injuries is not None else empty_injuries(),
            {},
        )
    )


def ref_of(row: dict[str, Any]) -> GameRef:
    return GameRef(
        league=row["league"],
        season=row["season"],
        game_id=row["game_id"],
        tip_utc=row["tip_utc"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        neutral=row["neutral"],
        postseason=row["postseason"],
        slate_date_et=row["slate_date_et"],
    )


def late_game(days: int = 40, seed: int = 31) -> tuple[pl.DataFrame, pl.DataFrame, GameRef]:
    games, box = season_frames(2025, days=days, seed=seed)
    row = games.sort("tip_utc").to_dicts()[-1]
    return games, box, ref_of(row)


def test_every_registered_feature_is_produced_and_nothing_else() -> None:
    games, box, ref = late_game()
    row = features_for_game(engine_for(games, box), ref, ref.tip_utc)
    assert list(row.features) == list(FEATURES)
    assert row.feature_set_version == FEATURE_SET_VERSION


def test_the_docs_groups_are_all_covered() -> None:
    names = set(FEATURES)
    for expected in (
        "adj_o_h", "adj_d_a", "adj_t_h", "elo_h", "elo_diff", "exp_poss", "exp_ppp_h",
        "exp_ppp_a", "exp_poss_1h", "exp_ppp_1h_h", "efg_o_h", "tov_d_a", "orb_o_h",
        "ftr_o_a", "par_o_h", "orb_matchup_h", "games_h", "prior_weight_a", "volatility_h",
        "neutral", "hca", "rest_h", "b2b_a", "games_7d_h", "travel_km_h", "days_since_start",
        "postseason", "form_h", "avail_missing_h", "avail_starters_a", "month", "is_weekend",
    ):  # fmt: skip
        assert expected in names, expected


def test_hash_is_stable_rounds_to_six_places_and_keeps_nulls() -> None:
    one = hash_features({"a": 1.00000001, "b": None})
    assert one == hash_features({"b": None, "a": 1.0})
    assert one != hash_features({"a": 1.1, "b": None})
    assert one != hash_features({"a": 1.0, "b": 0.0})
    assert len(one) == 64


def test_expected_ppp_uses_the_formula_and_home_edge() -> None:
    games, box, ref = late_game(days=60)
    engine = engine_for(games, box)
    snap = engine.ratings_as_of(2025, ref.tip_utc)
    features = features_for_game(engine, ref, ref.tip_utc).features
    league = snap.league
    edge = 0.0 if ref.neutral else league["hca"] / 2
    expected_home = league["mu"] + snap.value("off", ref.home_id) - snap.value("def", ref.away_id)
    assert features["exp_ppp_h"] == pytest.approx(expected_home + edge, abs=1e-5)
    expected_away = league["mu"] + snap.value("off", ref.away_id) - snap.value("def", ref.home_id)
    assert features["exp_ppp_a"] == pytest.approx(expected_away - edge, abs=1e-5)
    assert features["exp_poss"] == pytest.approx(
        league["tau"] + snap.value("tempo", ref.home_id) + snap.value("tempo", ref.away_id),
        abs=1e-5,
    )


def test_neutral_games_get_no_home_edge() -> None:
    games, box, ref = late_game()
    engine = engine_for(games, box)
    home_game = features_for_game(engine, ref_with(ref, neutral=False), ref.tip_utc).features
    neutral = features_for_game(engine, ref_with(ref, neutral=True), ref.tip_utc).features
    assert neutral["neutral"] == 1.0
    assert home_game["exp_ppp_h"] is not None and neutral["exp_ppp_h"] is not None
    assert home_game["exp_ppp_a"] is not None and neutral["exp_ppp_a"] is not None
    assert home_game["exp_ppp_h"] > neutral["exp_ppp_h"]
    assert home_game["exp_ppp_a"] < neutral["exp_ppp_a"]


def ref_with(ref: GameRef, **changes: Any) -> GameRef:
    return GameRef(**{**ref.__dict__, **changes})


def test_first_game_of_a_team_has_null_rest_and_form() -> None:
    games, box = season_frames(2025, days=5, seed=32)
    row = games.sort("tip_utc").to_dicts()[0]
    features = features_for_game(engine_for(games, box), ref_of(row), row["tip_utc"]).features
    assert features["rest_h"] is None
    assert features["form_h"] is None
    assert features["volatility_h"] is None
    assert features["games_h"] == 0.0
    assert features["prior_weight_h"] == 1.0
    assert features["days_since_start"] == 0.0


def test_rest_is_capped_and_back_to_back_is_flagged() -> None:
    games, box = season_frames(2025, teams=2, days=1, seed=33)
    first = games.to_dicts()[0]

    def later(days: float) -> GameRef:
        return ref_with(
            ref_of(first),
            game_id="next",
            tip_utc=first["tip_utc"] + timedelta(days=days),
        )

    engine = engine_for(games, box)
    near = later(1.0)
    near_features = features_for_game(engine, near, near.tip_utc).features
    assert near_features["rest_h"] == 1.0
    assert near_features["b2b_h"] == 1.0
    assert near_features["games_7d_h"] == 1.0
    far = later(20.0)
    far_features = features_for_game(engine, far, far.tip_utc).features
    assert far_features["rest_h"] == 7.0
    assert far_features["b2b_h"] == 0.0
    assert far_features["games_7d_h"] == 0.0


def test_calendar_uses_eastern_time() -> None:
    games, box = season_frames(2025, teams=2, days=1, seed=34)
    ref = ref_with(
        ref_of(games.to_dicts()[0]),
        game_id="x",
        tip_utc=datetime(2025, 3, 2, 2, 0, tzinfo=UTC),  # Saturday night, Eastern.
        slate_date_et=date(2025, 3, 1),
    )
    features = features_for_game(engine_for(games, box), ref, ref.tip_utc).features
    assert features["month"] == 3.0
    assert features["is_weekend"] == 1.0


def players_for(games: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for game in games.to_dicts():
        rows += [
            {"game_id": game["game_id"], "team_id": game["home_id"], "player_id": "star",
             "minutes": 36, "pts": 25, "starter": True},
            {"game_id": game["game_id"], "team_id": game["home_id"], "player_id": "bench",
             "minutes": 12, "pts": 4, "starter": False},
        ]  # fmt: skip
    return pl.DataFrame(rows, schema=PLAYER_SCHEMA)


def injuries_frame(team: str, player: str, status: str, when: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "captured_at": when,
                "player_id": player,
                "team_id": team,
                "status": status,
                "detail": "",
            }
        ],
        schema=empty_injuries().schema,
    )


def single_team_setup() -> tuple[pl.DataFrame, pl.DataFrame, GameRef]:
    games, box = season_frames(2025, teams=2, days=10, seed=35)
    # Make T00 the home side everywhere, so the two players have minutes for it.
    games = games.with_columns(pl.lit("T00").alias("home_id"), pl.lit("T01").alias("away_id"))
    row = games.sort("tip_utc").to_dicts()[-1]
    return games, box, ref_of(row)


def test_availability_is_null_without_a_snapshot_d15() -> None:
    games, box, ref = single_team_setup()
    features = features_for_game(
        engine_for(games, box, players_for(games)), ref, ref.tip_utc
    ).features
    assert features["avail_missing_h"] is None
    assert features["avail_starters_h"] is None


def test_availability_weights_a_players_share_of_minutes() -> None:
    games, box, ref = single_team_setup()
    when = ref.tip_utc - timedelta(hours=5)
    engine = engine_for(games, box, players_for(games), injuries_frame("T00", "star", "Out", when))
    features = features_for_game(engine, ref, ref.tip_utc).features
    assert features["avail_missing_h"] == pytest.approx(36 / 48)
    assert features["avail_starters_h"] == pytest.approx(36 / 48)
    assert features["avail_missing_a"] is None  # No minutes known for that team here.

    doubtful = engine_for(
        games, box, players_for(games), injuries_frame("T00", "bench", "Doubtful", when)
    )
    features = features_for_game(doubtful, ref, ref.tip_utc).features
    assert features["avail_missing_h"] == pytest.approx(0.75 * 12 / 48)
    assert features["avail_starters_h"] == 0.0


def test_availability_ignores_snapshots_taken_after_as_of_or_long_before() -> None:
    games, box, ref = single_team_setup()
    after = injuries_frame("T00", "star", "Out", ref.tip_utc + timedelta(minutes=1))
    late = features_for_game(engine_for(games, box, players_for(games), after), ref, ref.tip_utc)
    assert late.features["avail_missing_h"] is None
    old = injuries_frame("T00", "star", "Out", ref.tip_utc - timedelta(days=10))
    stale = features_for_game(engine_for(games, box, players_for(games), old), ref, ref.tip_utc)
    assert stale.features["avail_missing_h"] is None


def test_an_unknown_injury_status_gives_null_rather_than_a_guess() -> None:
    games, box, ref = single_team_setup()
    when = ref.tip_utc - timedelta(hours=5)
    engine = engine_for(
        games, box, players_for(games), injuries_frame("T00", "star", "Mystery", when)
    )
    assert features_for_game(engine, ref, ref.tip_utc).features["avail_missing_h"] is None


def test_availability_stays_out_of_v1_training_d15() -> None:
    assert {"avail_missing_h", "avail_missing_a", "avail_starters_h", "avail_starters_a"} == set(
        UNTRAINED_FEATURES
    )


def test_ncaa_availability_is_always_null() -> None:
    games, box = season_frames(2025, teams=4, days=10, seed=36, league="NCAA")
    row = games.sort("tip_utc").to_dicts()[-1]
    features = features_for_game(
        engine_for(games, box, league="NCAA"), ref_of(row), row["tip_utc"]
    ).features
    assert features["avail_missing_h"] is None


@pytest.mark.parametrize(
    ("league", "slate", "expected"),
    [
        ("NCAA", date(2025, 1, 18), datetime(2025, 1, 18, 14, 0, tzinfo=UTC)),
        ("NBA", date(2025, 1, 18), datetime(2025, 1, 18, 18, 30, tzinfo=UTC)),
    ],
)
def test_slate_run_time_is_lagos_time_on_the_slate_date(
    league: str, slate: date, expected: datetime
) -> None:
    assert slate_run_time(league, slate) == expected


def seed_tables(data_dir: Path, games: pl.DataFrame, box: pl.DataFrame) -> None:
    for season in games["season"].unique().to_list():
        rows = games.filter(pl.col("season") == season)
        write_parquet(rows, data_dir / f"curated/games/league=NBA/season={season}/part-0.parquet")
        write_parquet(
            box.filter(pl.col("game_id").is_in(rows["game_id"].to_list())),
            data_dir / f"curated/team_games/league=NBA/season={season}/part-0.parquet",
        )


def test_training_and_live_share_one_code_path(tmp_path: Path) -> None:
    """Docs/04's goal: a training row must equal what live prediction computes."""
    games, box = season_frames(2025, days=25, seed=37)
    seed_tables(tmp_path, games, box)
    summary = build_training_set(tmp_path, "NBA", [2025], log=lambda _: None)
    assert summary[2025]["rows"] == len(games)
    built = pl.read_parquet(tmp_path / "curated/features/league=NBA/season=2025.parquet")
    assert built["game_id"].n_unique() == len(games)

    engine = engine_for(games, box)
    for row in games.sort("tip_utc").to_dicts()[::30]:
        ref = ref_of(row)
        as_of = min(slate_run_time("NBA", row["slate_date_et"]), row["tip_utc"])
        live = features_for_game(engine, ref, as_of)
        stored = built.filter(pl.col("game_id") == row["game_id"]).to_dicts()[0]
        assert stored["feature_hash"] == live.feature_hash
        assert stored["as_of"] == as_of
        assert stored["feature_set_version"] == FEATURE_SET_VERSION
        assert stored["target_home_pts"] == row["home_pts"]
        assert stored["target_away_1h"] == row["away_1h"]
        assert stored["target_ot"] == 0


def test_training_runs_incrementally(tmp_path: Path) -> None:
    games, box = season_frames(2025, days=20, seed=38)
    cut = games.sort("tip_utc")["slate_date_et"].to_list()[-1]
    early = games.filter(pl.col("slate_date_et") < cut)
    seed_tables(tmp_path, early, box.filter(pl.col("game_id").is_in(early["game_id"].to_list())))
    build_training_set(tmp_path, "NBA", [2025], log=lambda _: None)
    first = pl.read_parquet(tmp_path / "curated/features/league=NBA/season=2025.parquet")

    seed_tables(tmp_path, games, box)
    summary = build_training_set(tmp_path, "NBA", [2025], log=lambda _: None)
    assert summary[2025]["new_rows"] == len(games) - len(early)
    second = pl.read_parquet(tmp_path / "curated/features/league=NBA/season=2025.parquet")
    assert len(second) == len(games)
    shared = second.filter(pl.col("game_id").is_in(first["game_id"].to_list())).sort("game_id")
    assert shared["feature_hash"].to_list() == first.sort("game_id")["feature_hash"].to_list()


def test_training_writes_one_snapshot_per_slate(tmp_path: Path) -> None:
    games, box = season_frames(2025, days=6, seed=39)
    seed_tables(tmp_path, games, box)
    build_training_set(tmp_path, "NBA", [2025], log=lambda _: None)
    folder = tmp_path / "curated/ratings_snapshots/league=NBA/season=2025"
    assert len(list(folder.glob("*.parquet"))) == 6
    snapshot = pl.read_parquet(next(folder.glob("*.parquet")))
    assert {"team_id", "off", "def", "tempo", "elo", "mu"} <= set(snapshot.columns)
