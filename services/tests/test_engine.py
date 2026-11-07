"""Leg eligibility, selection and ticket building (docs/06, invariants 2, 3, 5, 6, D3-D5, D10)."""

import json
import math
import os
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from hoopsedge.engine import (
    FAMILIES,
    BuildResult,
    Confidence,
    InjuryStatus,
    Reason,
    build_tickets,
    load_engine_config,
)
from hoopsedge.engine.build import main_line
from hoopsedge.models.registry import load_calibrators
from tests import engine_data
from tests.engine_data import IDENTITY, NOW, TIP, config, offer, period, prediction
from tests.engine_data import settings as market_settings

GOLDEN = Path(__file__).parent / "fixtures/engine/golden-tickets.json"


def _reasons(result: BuildResult) -> Counter[Reason]:
    return Counter(item.reason for item in result.rejections)


def _slate(
    count: int, league: str = "NCAA", family: str = "total_ft"
) -> tuple[list[Any], list[Any]]:
    """`count` games, each strong enough to pass, with the strongest being the lowest number."""
    predictions = [
        prediction(f"g{i:03d}", league, ft=period(120.0 - i * 0.01, 110.0)) for i in range(count)
    ]
    offers = [offer(f"g{i:03d}", family, league=league) for i in range(count)]
    return predictions, offers


def test_a_strong_leg_makes_a_traceable_ticket() -> None:
    pair = _slate(2)
    result = build_tickets(*pair, config(), NOW)
    assert len(result.tickets) == 1
    ticket = result.tickets[0]
    leg = ticket.legs[0]
    assert (leg.side, leg.rank, leg.game_id) == ("over", 1, "g000")
    # Invariant 6: the model, features and odds snapshot behind every prediction.
    assert leg.model_version == "20261106-1814-abc1234"
    assert leg.feature_hash == "hash-g000"
    assert leg.offer_fetched_at == NOW
    assert leg.fair == pytest.approx(0.5)
    assert leg.edge == pytest.approx(leg.p_calibrated - 0.5)


def test_ticket_totals_multiply_legs_and_record_the_assumption() -> None:
    result = build_tickets(*_slate(3), config(), NOW)
    ticket = result.tickets[0]
    assert ticket.combined_odds == pytest.approx(math.prod(leg.odds for leg in ticket.legs))
    assert ticket.combined_prob == pytest.approx(math.prod(leg.p_win for leg in ticket.legs))
    assert ticket.expected_value == pytest.approx(ticket.combined_prob * ticket.combined_odds - 1)
    assert any("independen" in text for text in result.assumptions)


# Eligibility: one test per code, each from a slate that otherwise passes.


def test_unmatched_events_are_excluded_never_guessed() -> None:
    result = build_tickets([prediction()], [offer(game_id=None)], config(), NOW)
    assert not result.tickets
    assert _reasons(result) == {Reason.UNMATCHED: 1}


@pytest.mark.parametrize("status", ["live", "ended", "suspended"])
def test_started_games_are_never_booked(status: str) -> None:
    result = build_tickets(*_slate(1), config(min_legs=1), NOW)
    assert result.tickets
    result = build_tickets([prediction()], [offer(status=status)], config(min_legs=1), NOW)
    assert not result.tickets
    assert _reasons(result) == {Reason.STARTED: 1}


@pytest.mark.parametrize(("minutes", "allowed"), [(0, False), (19, False), (20, True), (21, True)])
def test_tip_must_be_at_least_the_minimum_away(minutes: int, allowed: bool) -> None:
    tip = NOW + timedelta(minutes=minutes)
    result = build_tickets([prediction()], [offer(tip_time=tip)], config(min_legs=1), NOW)
    assert bool(result.tickets) is allowed
    if not allowed:
        assert _reasons(result) == {Reason.TOO_CLOSE: 1}


@pytest.mark.parametrize(("age", "allowed"), [(0, True), (300, True), (301, False)])
def test_odds_older_than_five_minutes_are_stale(age: int, allowed: bool) -> None:
    fetched = NOW - timedelta(seconds=age)
    result = build_tickets([prediction()], [offer(fetched_at=fetched)], config(min_legs=1), NOW)
    assert bool(result.tickets) is allowed
    if not allowed:
        assert _reasons(result) == {Reason.STALE_ODDS: 1}


def test_a_disabled_market_gives_no_legs() -> None:
    markets = config().markets
    markets["NCAA"]["total_ft"] = market_settings().model_copy(update={"enabled": False})
    result = build_tickets([prediction()], [offer()], config(markets=markets), NOW)
    assert _reasons(result) == {Reason.MARKET_DISABLED: 1}


def test_a_disabled_league_gives_no_legs() -> None:
    leagues = config().leagues
    leagues["NCAA"] = leagues["NCAA"].model_copy(update={"enabled": False})
    result = build_tickets([prediction()], [offer()], config(leagues=leagues), NOW)
    assert _reasons(result) == {Reason.MARKET_DISABLED: 1}


def test_a_market_with_no_threshold_gives_no_legs_and_says_why() -> None:
    # D69: NBA Full-time O/U has no threshold at any floor.
    markets = config().markets
    markets["NBA"]["total_ft"] = market_settings(None, None)
    result = build_tickets(
        [prediction(league="NBA")], [offer(league="NBA")], config(markets=markets), NOW
    )
    assert not result.tickets
    assert _reasons(result) == {Reason.NO_THRESHOLD: 1}


def test_low_probability_and_low_edge_are_told_apart() -> None:
    near = [prediction(ft=period(100.0, 100.0))]
    result = build_tickets(near, [offer()], config(), NOW)
    assert _reasons(result) == {Reason.LOW_PROB: 2}
    markets = config().markets
    markets["NCAA"]["total_ft"] = market_settings(0.5, 0.45)
    result = build_tickets([prediction()], [offer()], config(markets=markets), NOW)
    assert Reason.LOW_EDGE in _reasons(result)


def _thin(games: int, weight: float, **changes: Any) -> Any:
    return prediction(
        confidence=Confidence(
            games_home=games, games_away=20, prior_weight_home=weight, prior_weight_away=0.1
        ),
        **changes,
    )


def test_thin_data_needs_a_stricter_bar() -> None:
    # About 53%: over the 52% bar, but under the 56% one for thin data.
    modest = period(101.0, 100.5, 12.0, 17.0)
    chosen = offer(line=200.0)
    result = build_tickets([_thin(2, 0.7, ft=modest)], [chosen], config(min_legs=1), NOW)
    assert not result.tickets
    assert Reason.LOW_CONFIDENCE in _reasons(result)
    strong = period(120.0, 110.0)
    assert build_tickets([_thin(2, 0.7, ft=strong)], [chosen], config(min_legs=1), NOW).tickets


@pytest.mark.parametrize(("games", "weight"), [(2, 0.5), (20, 0.7)])
def test_low_confidence_needs_both_few_games_and_a_heavy_prior(games: int, weight: float) -> None:
    modest = period(101.0, 100.5, 12.0, 17.0)
    result = build_tickets(
        [_thin(games, weight, ft=modest)], [offer(line=200.0)], config(min_legs=1), NOW
    )
    assert result.tickets


def test_missing_period_or_prediction_is_recorded() -> None:
    no_half = [prediction(h1=None)]
    result = build_tickets(no_half, [offer(family="total_1h", line=107.5)], config(), NOW)
    assert _reasons(result) == {Reason.MISSING_PRED: 1}
    result = build_tickets([], [offer()], config(), NOW)
    assert _reasons(result) == {Reason.MISSING_PRED: 1}


def _nba(**changes: Any) -> Any:
    return prediction(league="NBA", **changes)


def test_nba_questionable_starter_on_an_old_snapshot_is_rejected() -> None:
    stale = InjuryStatus(questionable_starter=True, snapshot_at=TIP - timedelta(hours=3, seconds=1))
    result = build_tickets([_nba(injury=stale)], [offer(league="NBA")], config(min_legs=1), NOW)
    assert _reasons(result) == {Reason.NBA_INJURY_UNCERTAIN: 1}


def test_nba_passes_with_a_fresh_snapshot_or_no_questionable_starter() -> None:
    edge = InjuryStatus(questionable_starter=True, snapshot_at=TIP - timedelta(hours=3))
    clear = InjuryStatus(questionable_starter=False, snapshot_at=NOW - timedelta(hours=10))
    for injury in (edge, clear):
        result = build_tickets(
            [_nba(injury=injury)], [offer(league="NBA")], config(min_legs=1), NOW
        )
        assert result.tickets


def test_nba_with_no_injury_snapshot_is_uncertain_but_ncaa_is_not_affected() -> None:
    # D15 relies on this gate because no injury feature exists, so "unknown" can't count as "clear".
    result = build_tickets([_nba(injury=None)], [offer(league="NBA")], config(min_legs=1), NOW)
    assert _reasons(result) == {Reason.NBA_INJURY_UNCERTAIN: 1}
    assert build_tickets([prediction(injury=None)], [offer()], config(min_legs=1), NOW).tickets


# Main line (D5, D32).


def test_main_line_is_the_most_even_bookable_line() -> None:
    lines = [
        offer(line=198.5, odds=(1.5, 2.6)),
        offer(line=200.5, odds=(1.9, 1.9)),
        offer(line=202.5, odds=(2.6, 1.5)),
    ]
    assert main_line(lines).line == 200.5
    assert main_line(list(reversed(lines))).line == 200.5


def test_main_line_ties_go_to_the_favourite_flag_then_the_lowest_line() -> None:
    plain = offer(line=199.5, odds=(1.75, 1.9))
    flagged = offer(line=200.5, odds=(1.9, 1.75), favourite=True)
    assert main_line([plain, flagged]).line == 200.5
    assert main_line([offer(line=200.5, odds=(1.9, 1.75)), plain]).line == 199.5


def test_a_suspended_line_is_never_main_even_if_most_even() -> None:
    locked = offer(line=200.5, odds=(1.9, 1.9), bookable=False)
    open_line = offer(line=202.5, odds=(1.6, 2.2))
    result = build_tickets(
        [prediction(ft=period(115.0, 105.0))], [locked, open_line], config(min_legs=1), NOW
    )
    assert [leg.line for ticket in result.tickets for leg in ticket.legs] == [202.5]


def test_a_market_with_nothing_bookable_says_so() -> None:
    result = build_tickets([prediction()], [offer(bookable=False)], config(), NOW)
    assert _reasons(result) == {Reason.NO_BOOKABLE_LINE: 1}


def test_alternate_lines_stay_out_until_allowed() -> None:
    lines = [offer(line=200.5), offer(line=170.5, odds=(1.2, 4.0))]
    strong = [prediction(ft=period(115.0, 105.0))]
    only_main = build_tickets(strong, lines, config(min_legs=1), NOW)
    assert only_main.tickets[0].legs[0].line == 200.5
    with_alts = build_tickets(strong, lines, config(min_legs=1, allow_alt_lines=True), NOW)
    # D10 still holds: the higher probability of the alternate line wins, one leg per game.
    assert [leg.line for leg in with_alts.tickets[0].legs] == [170.5]
    assert _reasons(with_alts)[Reason.SAME_GAME] == 1


# Selection and partitioning.


def test_team_total_keeps_only_the_best_of_four_outcomes() -> None:
    legs = [
        offer(family="team_total", team="home", line=100.5),
        offer(family="team_total", team="away", line=110.5),
    ]
    result = build_tickets([prediction(ft=period(115.0, 101.0))], legs, config(min_legs=1), NOW)
    assert len(result.tickets[0].legs) == 1
    assert (result.tickets[0].legs[0].team, result.tickets[0].legs[0].side) in {
        ("home", "over"),
        ("away", "under"),
    }
    assert _reasons(result)[Reason.SAME_GAME] == 1


def test_handicap_orientation_follows_the_home_line() -> None:
    # Model has home by 5. Home -1.5 should be the home side, and home -12.5 the away side.
    short = build_tickets(
        [prediction()], [offer(family="handicap", line=-1.5)], config(min_legs=1), NOW
    )
    assert short.tickets[0].legs[0].side == "home"
    long = build_tickets(
        [prediction()], [offer(family="handicap", line=-12.5)], config(min_legs=1), NOW
    )
    assert long.tickets[0].legs[0].side == "away"


@pytest.mark.parametrize(
    ("count", "sizes"),
    [
        (0, []),
        (1, []),
        (19, [19]),
        (20, [20]),
        (21, [20]),  # The 21st alone is under min_legs.
        (22, [20, 2]),
        (99, [20, 20, 20, 20, 19]),
        (100, [20, 20, 20, 20, 20]),
        (101, [20, 20, 20, 20, 20]),
        (250, [20, 20, 20, 20, 20]),
    ],
)
def test_ncaa_partitions_top_100_into_tickets_of_20(count: int, sizes: list[int]) -> None:
    result = build_tickets(*_slate(count), config(), NOW)
    assert [len(ticket.legs) for ticket in result.tickets] == sizes
    assert [ticket.ticket_no for ticket in result.tickets] == list(range(1, len(sizes) + 1))
    used = sum(sizes)
    reasons = _reasons(result)
    assert reasons[Reason.BELOW_CUTOFF] == max(0, count - 100)
    assert reasons[Reason.TICKET_TOO_SHORT] == min(count, 100) - used


@pytest.mark.parametrize(("count", "size"), [(1, 0), (2, 2), (20, 20), (21, 20), (100, 20)])
def test_nba_makes_one_ticket_per_market(count: int, size: int) -> None:
    result = build_tickets(*_slate(count, "NBA"), config(), NOW)
    assert [len(ticket.legs) for ticket in result.tickets] == ([size] if size else [])


def test_tickets_are_in_rank_order_across_the_market() -> None:
    result = build_tickets(*_slate(45), config(), NOW)
    ranks = [leg.rank for ticket in result.tickets for leg in ticket.legs]
    assert ranks == list(range(1, 46))
    assert [leg.game_id for leg in result.tickets[0].legs] == [f"g{i:03d}" for i in range(20)]
    chance = [leg.p_calibrated for ticket in result.tickets for leg in ticket.legs]
    assert chance == sorted(chance, reverse=True)


def test_each_market_is_its_own_set_of_tickets() -> None:
    predictions, _ = _slate(4)
    offers = [offer(f"g{i:03d}", "total_ft") for i in range(4)]
    offers += [offer(f"g{i:03d}", "total_1h", line=100.5) for i in range(4)]
    result = build_tickets(predictions, offers, config(), NOW)
    assert {ticket.family for ticket in result.tickets} == {"total_ft", "total_1h"}
    assert not _reasons(result)[Reason.SAME_GAME]


def test_tickets_carry_the_gate_verdict_for_the_dashboard() -> None:
    # D65: a market in force that failed its gate has to stay visible.
    markets = config().markets
    markets["NCAA"]["total_ft"] = market_settings().model_copy(
        update={"passed_gate": False, "edge_label": "unproven"}
    )
    ticket = build_tickets(*_slate(2), config(markets=markets), NOW).tickets[0]
    assert ticket.passed_gate is False
    assert ticket.edge_label == "unproven"


def test_selection_hash_ignores_leg_order_and_changes_with_the_selection() -> None:
    first = build_tickets(*_slate(3), config(), NOW).tickets[0]
    predictions, offers = _slate(3)
    second = build_tickets(list(reversed(predictions)), offers[::-1], config(), NOW).tickets[0]
    third = build_tickets(*_slate(4), config(), NOW).tickets[0]
    assert first.selection_hash == second.selection_hash
    assert first.selection_hash != third.selection_hash


# Fail loudly (invariant 7).


def test_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        build_tickets([], [], config(), NOW.replace(tzinfo=None))


def test_duplicates_are_refused_rather_than_silently_chosen() -> None:
    with pytest.raises(ValueError, match="g1"):
        build_tickets([prediction(), prediction()], [], config(), NOW)
    with pytest.raises(ValueError, match="sr:match:g1"):
        build_tickets([prediction()], [offer(), offer()], config(), NOW)


def test_a_live_market_with_no_calibrator_stops_the_build() -> None:
    calibrators: dict[str, dict[str, Any]] = {"NBA": {}, "NCAA": {}}
    with pytest.raises(ValueError, match="calibrator"):
        build_tickets([prediction()], [offer()], config(calibrators=calibrators), NOW)


@pytest.mark.parametrize(
    "bad",
    [
        {"team": "home"},
        {"family": "team_total", "team": None},
        {"family": "handicap", "line": -1.5, "league": "EPL"},
        {"tip_time": TIP.replace(tzinfo=None)},
    ],
)
def test_malformed_offers_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        offer(**{"market_id": "225", **bad})


def test_odds_must_be_above_one_and_sides_must_fit_the_market() -> None:
    with pytest.raises(ValidationError):
        offer(odds=(1.0, 1.9))
    with pytest.raises(ValidationError):
        offer(family="total_ft", outcomes=offer(family="handicap", line=1.5).outcomes)


# Determinism and invariants across arbitrary slates.

game = st.tuples(
    st.floats(90, 130), st.floats(90, 130), st.sampled_from([190.5, 200.0, 210.5]),
    st.sampled_from([(1.91, 1.91), (1.7, 2.1), (2.1, 1.7)]),
)  # fmt: skip


def _arbitrary(games: list[tuple[float, float, float, tuple[float, float]]]) -> tuple[Any, Any]:
    predictions, offers = [], []
    for number, (home, away, line, odds) in enumerate(games):
        name = f"g{number:03d}"
        predictions.append(prediction(name, ft=period(home, away)))
        offers.append(offer(name, "total_ft", line=line, odds=odds))
        offers.append(offer(name, "handicap", line=-(line - 200.0), odds=odds))
        offers.append(offer(name, "team_total", team="home", line=line / 2, odds=odds))
        offers.append(offer(name, "team_total", team="away", line=line / 2, odds=odds))
    return predictions, offers


@settings(max_examples=30, deadline=None)
@given(st.lists(game, max_size=30), st.randoms(use_true_random=False))
def test_shuffled_input_gives_identical_output(games: list[Any], rng: random.Random) -> None:
    predictions, offers = _arbitrary(games)
    first = build_tickets(predictions, offers, config(), NOW)
    rng.shuffle(predictions)
    rng.shuffle(offers)
    assert build_tickets(predictions, offers, config(), NOW) == first


@settings(max_examples=30, deadline=None)
@given(st.lists(game, max_size=30))
def test_ticket_invariants_hold_on_any_slate(games: list[Any]) -> None:
    predictions, offers = _arbitrary(games)
    active = config()
    result = build_tickets(predictions, offers, active, NOW)
    seen: Counter[tuple[str, str]] = Counter()
    for ticket in result.tickets:
        assert active.min_legs <= len(ticket.legs) <= 20
        # D10: one leg per game per ticket.
        assert len({leg.game_id for leg in ticket.legs}) == len(ticket.legs)
        for leg in ticket.legs:
            # Invariant 2 and thresholds: nothing weak or started got through.
            assert leg.tip_time >= NOW + timedelta(minutes=active.min_minutes_before_tip)
            threshold = active.markets[ticket.league][ticket.family]
            assert threshold.min_prob is not None and threshold.min_edge is not None
            assert leg.p_calibrated >= threshold.min_prob
            assert leg.edge >= threshold.min_edge
            seen[(ticket.family, leg.game_id)] += 1
    # No leg or game in two tickets of a market.
    assert all(count == 1 for count in seen.values())


@settings(max_examples=30, deadline=None)
@given(st.lists(game, min_size=1, max_size=20))
def test_every_outcome_is_used_or_has_a_reason(games: list[Any]) -> None:
    predictions, offers = _arbitrary(games)
    result = build_tickets(predictions, offers, config(), NOW)
    used = {
        (leg.game_id, leg.market_id, leg.side) for ticket in result.tickets for leg in ticket.legs
    }
    rejected = {(item.game_id, item.market_id, item.side) for item in result.rejections}
    assert not used & rejected
    assert len(used) + len(rejected) == sum(len(item.outcomes) for item in offers)
    assert all(isinstance(item.reason, Reason) for item in result.rejections)


# Settings from Phase 4's files.


def _write_params(folder: Path) -> None:
    gate = {"enabled": True, "passed": False, "edge": "unproven"}
    gates = {
        league: {family: gate for family in ("handicap", "total_ft", "total_1h", "team_total")}
        for league in ("NBA", "NCAA")
    }
    gates["NCAA"]["total_1h"] = {"enabled": False, "passed": False, "edge": "measured"}
    chosen = {"min_prob": 0.53, "min_edge": 0.01, "legs": 5, "legs_per_slate": 1.0}
    thresholds = {
        league: {"handicap": chosen, "total_ft": None, "total_1h": chosen, "team_total": chosen}
        for league in ("NBA", "NCAA")
    }
    (folder / "market_gates.yaml").write_text(yaml.safe_dump(gates), "utf-8")
    (folder / "thresholds.yaml").write_text(yaml.safe_dump(thresholds), "utf-8")
    shrink = {"model_weight": {"measured": 1.0, "unproven": 1.0}, "max_disagreement_sd": None}
    (folder / "market_shrink.yaml").write_text(yaml.safe_dump(shrink), "utf-8")


def test_settings_come_from_the_gate_and_threshold_files(tmp_path: Path) -> None:
    _write_params(tmp_path)
    calibrators = {league: {"handicap": IDENTITY} for league in ("NBA", "NCAA")}
    loaded = load_engine_config(calibrators, tmp_path)
    assert loaded.markets["NCAA"]["handicap"].min_prob == 0.53
    assert loaded.markets["NCAA"]["handicap"].passed_gate is False
    assert loaded.markets["NCAA"]["handicap"].edge_label == "unproven"
    assert loaded.markets["NBA"]["total_ft"].min_prob is None
    assert loaded.markets["NCAA"]["total_1h"].enabled is False
    assert loaded.min_legs == 2
    assert loaded.leagues["NCAA"].max_legs_per_market == 100
    assert loaded.leagues["NBA"].max_tickets_per_market == 1


def test_a_missing_settings_file_stops_the_load(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_engine_config({}, tmp_path)


def test_a_market_missing_from_the_files_stops_the_load(tmp_path: Path) -> None:
    _write_params(tmp_path)
    (tmp_path / "thresholds.yaml").write_text(yaml.safe_dump({"NBA": {}, "NCAA": {}}), "utf-8")
    with pytest.raises(ValueError, match="thresholds"):
        load_engine_config({}, tmp_path)


def test_the_real_settings_files_load() -> None:
    loaded = load_engine_config({})
    assert set(loaded.markets) == {"NBA", "NCAA"}
    assert all(len(markets) == 4 for markets in loaded.markets.values())


def test_calibrators_load_by_family_and_a_build_without_them_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_calibrators(tmp_path)
    (tmp_path / "calibrators").mkdir()
    (tmp_path / "calibrators/handicap.json").write_text(json.dumps(IDENTITY.to_json()), "utf-8")
    assert load_calibrators(tmp_path) == {"handicap": IDENTITY}


# Golden file: real SportyBet offers, with fixed predictions.


def _stable(value: Any) -> Any:
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _golden_result() -> dict[str, Any]:
    offers = engine_data.recorded_offers(NOW)
    first_tip = min(item.tip_time for item in offers)
    now = first_tip - timedelta(hours=1)
    offers = [item.model_copy(update={"fetched_at": now}) for item in offers]
    predictions = engine_data.recorded_predictions(offers, now)
    result = build_tickets(predictions, offers, config(), now)
    stable: dict[str, Any] = _stable(
        {
            "now": now.isoformat(),
            "offers": len(offers),
            "predictions": len(predictions),
            "rejections": dict(sorted(Counter(r.reason.value for r in result.rejections).items())),
            "tickets": [ticket.model_dump(mode="json") for ticket in result.tickets],
        }
    )
    return stable


def test_a_recorded_real_slate_gives_stable_tickets() -> None:
    result = _golden_result()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", "utf-8")
    assert result == json.loads(GOLDEN.read_text("utf-8"))
    assert result["tickets"], "The recorded slate should give at least one ticket"
    assert {ticket["league"] for ticket in result["tickets"]} == {"NBA", "NCAA"}


# D78: every market can fill, and a leg below what testing chose says so.


def test_every_market_runs_on_the_owners_minimum_and_remembers_what_testing_chose() -> None:
    families = {family: IDENTITY for family in FAMILIES}
    loaded = load_engine_config({"NBA": families, "NCAA": families})
    for league in ("NBA", "NCAA"):
        for family in FAMILIES:
            market = loaded.markets[league][family]
            assert (market.min_prob, market.min_edge) == (0.5, 0.0), (league, family)
            assert market.has_tested_floor
    assert loaded.markets["NBA"]["total_ft"].tested_min_prob is None  # Testing found none (D69).
    assert loaded.markets["NBA"]["handicap"].tested_min_prob == 0.52
    assert loaded.markets["NCAA"]["team_total"].tested_min_prob == 0.64


def _weak_counts(**tested: Any) -> list[bool]:
    predictions, offers = _slate(3)
    market = market_settings(min_prob=0.5).model_copy(update={"has_tested_floor": True, **tested})
    markets = {league: {family: market for family in FAMILIES} for league in ("NBA", "NCAA")}
    result = build_tickets(predictions, offers, config(markets=markets), NOW)
    return [leg.below_tested for ticket in result.tickets for leg in ticket.legs]


def test_a_leg_below_the_tested_minimum_is_marked() -> None:
    assert _weak_counts(tested_min_prob=0.99, tested_min_edge=0.0) == [True] * 3


def test_a_leg_above_the_tested_minimum_is_not_marked() -> None:
    assert _weak_counts(tested_min_prob=0.5, tested_min_edge=-1.0) == [False] * 3


def test_a_market_testing_found_no_minimum_for_marks_every_leg() -> None:
    assert _weak_counts(tested_min_prob=None, tested_min_edge=None) == [True] * 3


def test_a_market_without_tested_figures_marks_nothing() -> None:
    predictions, offers = _slate(3)
    result = build_tickets(predictions, offers, config(), NOW)
    assert not any(leg.below_tested for ticket in result.tickets for leg in ticket.legs)
