"""D79: lessons from the first real slate (2026-11-06).

The model's probabilities lean toward the bookmaker's fair price, more so where it has no real
results; a model that disagrees wildly with the line is not trusted; early-season NCAA is capped.
"""

from collections import Counter
from typing import Any

import pytest

from hoopsedge.engine import Confidence, Reason, build_tickets, load_engine_config
from hoopsedge.engine.recheck import reprice_leg
from hoopsedge.markets.probability import SideProbability, toward_market
from tests.engine_data import IDENTITY, NOW, config, offer, period, prediction
from tests.engine_data import settings as market_settings

FAMILIES = ("handicap", "total_ft", "total_1h", "team_total")


def _reasons(result: Any) -> Counter[Reason]:
    return Counter(item.reason for item in result.rejections)


def _markets(label: str = "unproven", **changes: Any) -> dict[str, dict[str, Any]]:
    market = market_settings(min_prob=0.5, min_edge=0.0).model_copy(
        update={"edge_label": label, **changes}
    )
    return {league: {family: market for family in FAMILIES} for league in ("NBA", "NCAA")}


def _one_leg(weight: float, label: str = "unproven", **changes: Any) -> Any:
    weights = {"measured": 1.0, "unproven": 1.0, label: weight}
    built = build_tickets(
        [prediction(ft=period(108.0, 100.0))],
        [offer(line=200.5, odds=(1.80, 2.00))],
        config(markets=_markets(label), model_weight=weights, min_legs=1, **changes),
        NOW,
    )
    return built


# The shrink itself.


def test_toward_market_moves_the_probability_by_the_weight_and_keeps_the_push() -> None:
    side = SideProbability(p_raw=0.7, p_calibrated=0.7, p_win=0.63, p_push=0.1, p_lose=0.27)
    shrunk = toward_market(side, fair=0.5, weight=0.25)
    assert shrunk.p_calibrated == pytest.approx(0.55)
    assert shrunk.p_raw == 0.7
    assert shrunk.p_push == 0.1
    assert shrunk.p_win == pytest.approx(0.55 * 0.9)
    assert shrunk.p_lose == pytest.approx(0.45 * 0.9)


@pytest.mark.parametrize("weight", [0.0, 0.25, 0.5, 1.0])
def test_a_leg_keeps_the_model_view_and_records_the_weight(weight: float) -> None:
    leg = _one_leg(weight).tickets[0].legs[0]
    assert leg.model_weight == weight
    assert leg.p_model is not None
    assert leg.p_calibrated == pytest.approx(leg.fair + weight * (leg.p_model - leg.fair))
    assert leg.edge == pytest.approx(weight * (leg.p_model - leg.fair))
    assert leg.p_win == pytest.approx(leg.p_calibrated * (1 - leg.p_push))


def test_full_weight_changes_nothing() -> None:
    leg = _one_leg(1.0).tickets[0].legs[0]
    assert leg.p_calibrated == pytest.approx(leg.p_model)


def test_measured_and_unproven_markets_get_their_own_weights() -> None:
    measured = _one_leg(0.5, "measured").tickets[0].legs[0]
    unproven = _one_leg(0.25, "unproven").tickets[0].legs[0]
    assert measured.model_weight == 0.5
    assert unproven.model_weight == 0.25
    assert unproven.p_calibrated < measured.p_calibrated


def test_a_weak_model_edge_falls_below_the_bar_once_shrunk() -> None:
    # The model says about 58% against a fair 50%. Shrunk to a quarter, 52%: under a 54% bar.
    weights = {"measured": 0.25, "unproven": 0.25}
    markets = _markets("measured", min_prob=0.54)
    result = build_tickets(
        [prediction(ft=period(105.0, 98.0))],
        [offer(line=200.5)],
        config(markets=markets, model_weight=weights, min_legs=1),
        NOW,
    )
    assert not result.tickets
    assert Reason.LOW_PROB in _reasons(result)


# A wild disagreement with the line is the model's mistake, not an edge.


def test_a_line_far_from_the_model_mean_is_rejected_on_both_sides() -> None:
    # Mean total 230 against 200.5 with sd 17: about 1.7 sd away.
    result = build_tickets(
        [prediction(ft=period(120.0, 110.0))],
        [offer(line=200.5)],
        config(max_disagreement_sd=1.0, min_legs=1),
        NOW,
    )
    assert not result.tickets
    rejected = [item for item in result.rejections if item.reason is Reason.MODEL_DISAGREES]
    assert len(rejected) == 1
    assert rejected[0].specifier == "total=200.5"


def test_a_line_near_the_model_mean_is_kept() -> None:
    result = build_tickets(
        [prediction(ft=period(108.0, 100.0))],
        [offer(line=200.5)],
        config(max_disagreement_sd=1.0, min_legs=1),
        NOW,
    )
    assert result.tickets


def test_handicap_disagreement_is_measured_against_the_home_line() -> None:
    # Home by 5 (110 against 105). A home -5.5 line is near; a home +20.5 line is far.
    near = offer(family="handicap", line=-5.5)
    far = offer(game_id="g2", family="handicap", line=20.5)
    result = build_tickets(
        [prediction(), prediction("g2")],
        [near, far],
        config(max_disagreement_sd=1.0, min_legs=1),
        NOW,
    )
    rejected = {item.game_id for item in result.rejections if item.reason is Reason.MODEL_DISAGREES}
    assert rejected == {"g2"}


def test_team_totals_use_the_team_spread() -> None:
    # Home mean 110 with sd 11: a 95.5 line is 1.3 sd away, a 106.5 line 0.3.
    far = offer(family="team_total", team="home", line=95.5)
    near = offer(game_id="g2", family="team_total", team="home", line=106.5)
    result = build_tickets(
        [prediction(), prediction("g2")],
        [far, near],
        config(max_disagreement_sd=1.0, min_legs=1),
        NOW,
    )
    rejected = {item.game_id for item in result.rejections if item.reason is Reason.MODEL_DISAGREES}
    assert rejected == {"g1"}


# Early-season NCAA: thin-data legs are capped per market.


def _thin_slate(count: int) -> tuple[list[Any], list[Any]]:
    thin = Confidence(games_home=1, games_away=1, prior_weight_home=0.9, prior_weight_away=0.9)
    predictions = [
        prediction(f"g{i:03d}", ft=period(120.0 - i * 0.01, 110.0), confidence=thin)
        for i in range(count)
    ]
    return predictions, [offer(f"g{i:03d}") for i in range(count)]


def test_thin_data_legs_are_capped_per_market() -> None:
    result = build_tickets(
        *_thin_slate(30), config(thin_data_max_legs_per_market={"NCAA": 20}), NOW
    )
    assert [len(ticket.legs) for ticket in result.tickets] == [20]
    assert _reasons(result)[Reason.THIN_DATA_CUTOFF] == 10
    assert all(leg.thin_data for ticket in result.tickets for leg in ticket.legs)


def test_the_cap_keeps_the_strongest_thin_legs_and_leaves_full_data_legs_alone() -> None:
    thin_predictions, thin_offers = _thin_slate(25)
    full = [prediction(f"f{i:03d}", ft=period(119.0, 110.0)) for i in range(5)]
    offers = thin_offers + [offer(f"f{i:03d}") for i in range(5)]
    result = build_tickets(
        thin_predictions + full, offers, config(thin_data_max_legs_per_market={"NCAA": 20}), NOW
    )
    legs = [leg for ticket in result.tickets for leg in ticket.legs]
    assert sum(leg.thin_data for leg in legs) == 20
    assert sum(not leg.thin_data for leg in legs) == 5
    kept = {leg.game_id for leg in legs if leg.thin_data}
    assert kept == {f"g{i:03d}" for i in range(20)}


def test_no_cap_for_a_league_without_one() -> None:
    result = build_tickets(*_thin_slate(30), config(thin_data_max_legs_per_market={"NBA": 5}), NOW)
    assert sum(len(ticket.legs) for ticket in result.tickets) == 30


# Invariant 3: a re-priced leg is re-scored with the same shrink.


def test_repricing_reapplies_the_shrink_to_the_new_fair_price() -> None:
    leg = _one_leg(0.25).tickets[0].legs[0]
    moved = offer(line=200.5, odds=(1.91, 1.91))
    fresh = reprice_leg(leg, moved)
    assert fresh.fair == pytest.approx(0.5)
    assert fresh.p_calibrated == pytest.approx(0.5 + 0.25 * (leg.p_model - 0.5))
    assert fresh.edge == pytest.approx(fresh.p_calibrated - 0.5)
    assert fresh.p_win == pytest.approx(fresh.p_calibrated * (1 - fresh.p_push))


# The real settings.


def test_the_real_settings_shrink_toward_the_market_and_cap_early_ncaa() -> None:
    families = {family: IDENTITY for family in FAMILIES}
    loaded = load_engine_config({"NBA": families, "NCAA": families})
    assert loaded.model_weight == {"measured": 0.5, "unproven": 0.25}
    assert loaded.max_disagreement_sd == 1.0
    assert loaded.thin_data_max_legs_per_market == {"NCAA": 20}
    assert loaded.low_confidence_extra_prob == pytest.approx(0.04)


def test_engine_defaults_stay_neutral_for_pure_tests() -> None:
    plain = config()
    assert plain.model_weight == {"measured": 1.0, "unproven": 1.0}
    assert plain.max_disagreement_sd is None
    assert plain.thin_data_max_legs_per_market == {}
