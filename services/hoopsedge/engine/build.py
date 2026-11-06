"""Predictions and offers in, tickets out: pure and deterministic (docs/06, D70).

No network, no clock and no randomness: `now` is an argument, and input order can't change a result.
"""

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from hoopsedge.engine.schemas import (
    FAMILIES,
    NOT_STARTED,
    BuildResult,
    EngineConfig,
    Leg,
    Offer,
    PeriodPrediction,
    Prediction,
    Reason,
    Rejection,
    ScoredLine,
    Ticket,
)
from hoopsedge.markets.probability import (
    SideProbability,
    expected_value,
    fair_probability,
    score_two_way,
)
from hoopsedge.models.distribution import ScoreDistribution

INDEPENDENCE = "combined_prob multiplies the legs' p_win, assuming independence across games."
EVEN_DECIMALS = 9  # Mirrored prices such as 1.75/1.90 and 1.90/1.75 must tie exactly.
FIRST_SIDES = {"home", "over"}


def main_line(lines: Sequence[Offer]) -> Offer:
    """The most even line. Ties go to `favourite`, then the lowest line (D32).

    D32's "first specifier" depends on response order, so the lowest line stands in for it.
    """

    def key(offer: Offer) -> tuple[float, bool, float, str]:
        first, second = offer.outcomes
        fair = fair_probability(first.odds, second.odds)
        return (
            round(abs(fair - 0.5), EVEN_DECIMALS),
            not offer.favourite,
            offer.line,
            offer.specifier,
        )

    return min(lines, key=key)


def _distribution(period: PeriodPrediction) -> ScoreDistribution:
    return ScoreDistribution(
        np.array([period.mu_home]),
        np.array([period.mu_away]),
        np.array([period.sd_margin]),
        np.array([period.sd_total]),
        np.array([period.sd_home]),
        np.array([period.sd_away]),
    )


def _chances(period: PeriodPrediction, offer: Offer) -> tuple[float, float, float]:
    """P(home or over), P(push), P(away or under)."""
    dist = _distribution(period)
    line = np.array([offer.line])
    if offer.family == "handicap":
        above, push, below = dist.handicap(line)
    elif offer.team is not None:
        above, push, below = dist.team_total(offer.team, line)
    else:
        above, push, below = dist.total(line)
    return float(above[0]), float(push[0]), float(below[0])


def _period_for(prediction: Prediction, offer: Offer) -> PeriodPrediction | None:
    return prediction.h1 if offer.family == "total_1h" else prediction.ft


def _predicted_mean(prediction: Prediction, offer: Offer) -> float | None:
    period = _period_for(prediction, offer)
    if period is None:
        return None
    if offer.family == "handicap":
        return period.mu_home - period.mu_away
    if offer.team == "home":
        return period.mu_home
    if offer.team == "away":
        return period.mu_away
    return period.mu_home + period.mu_away


def score_offers(predictions: Sequence[Prediction], offers: Sequence[Offer]) -> list[ScoredLine]:
    """Every bookable main line with a prediction, whatever the thresholds and gates say.

    Real-line calibrators need the lines nothing was picked from too, or they'd only ever see
    where the model most disagrees with the market (docs/08 section 7.3, C5).
    """
    by_game = {item.game_id: item for item in predictions}
    markets: dict[tuple[str, str, str], list[Offer]] = {}
    for offer in offers:
        if offer.game_id is not None and offer.game_id in by_game and offer.bookable:
            markets.setdefault((offer.event_id, offer.family, offer.team or ""), []).append(offer)
    scored: list[ScoredLine] = []
    for key in sorted(markets):
        offer = main_line(markets[key])
        assert offer.game_id is not None  # noqa: S101 - Filtered just above.
        prediction = by_game[offer.game_id]
        period = _period_for(prediction, offer)
        if period is None:
            continue
        above, push, _ = _chances(period, offer)
        first = next(item for item in offer.outcomes if item.side in FIRST_SIDES)
        second = next(item for item in offer.outcomes if item.side not in FIRST_SIDES)
        scored.append(
            ScoredLine(
                league=offer.league,
                game_id=offer.game_id,
                event_id=offer.event_id,
                family=offer.family,
                market_id=offer.market_id,
                specifier=offer.specifier,
                line=offer.line,
                team=offer.team,
                p_above=above,
                p_push=push,
                fair_above=fair_probability(first.odds, second.odds),
                tip_time=offer.tip_time,
                fetched_at=offer.fetched_at,
                model_version=prediction.model_version,
            )
        )
    return scored


def _market_reason(offer: Offer, config: EngineConfig, now: datetime) -> Reason | None:
    """What rules out a market on this event, before looking at any of its lines."""
    if offer.game_id is None:
        return Reason.UNMATCHED
    if offer.status != NOT_STARTED:
        return Reason.STARTED
    if offer.tip_time < now + timedelta(minutes=config.min_minutes_before_tip):
        return Reason.TOO_CLOSE
    market = config.markets[offer.league][offer.family]
    if not (market.enabled and config.leagues[offer.league].enabled):
        return Reason.MARKET_DISABLED
    if market.min_prob is None or market.min_edge is None:
        return Reason.NO_THRESHOLD
    return None


def _injury_uncertain(prediction: Prediction, offer: Offer, config: EngineConfig) -> bool:
    if prediction.league != "NBA":
        return False
    injury = prediction.injury
    if injury is None:
        return True  # D15 relies on this gate, so "no information" can't pass as "nobody hurt".
    limit = offer.tip_time - timedelta(hours=config.injury_snapshot_hours_before_tip)
    return injury.questionable_starter and injury.snapshot_at < limit


def _thin_data(prediction: Prediction, config: EngineConfig) -> bool:
    fewest = config.min_team_games_for_full_confidence[prediction.league]
    limit = config.low_confidence_prior_weight
    confidence = prediction.confidence
    return (confidence.games_home < fewest and confidence.prior_weight_home > limit) or (
        confidence.games_away < fewest and confidence.prior_weight_away > limit
    )


def _rank_key(leg: Leg) -> tuple[float, float, str, str, str, str]:
    return (
        -leg.p_calibrated,
        -leg.edge,
        leg.event_id,
        leg.market_id,
        leg.specifier,
        leg.outcome_id,
    )


@dataclass
class _Build:
    predictions: dict[str, Prediction]
    config: EngineConfig
    now: datetime
    legs: list[Leg]
    rejections: list[Rejection]

    def reject(self, offer: Offer, reason: Reason) -> None:
        self.rejections.append(
            Rejection(
                league=offer.league,
                game_id=offer.game_id,
                event_id=offer.event_id,
                family=offer.family,
                reason=reason,
            )
        )

    def reject_line(self, offer: Offer, reason: Reason) -> None:
        self.rejections.append(
            Rejection(
                league=offer.league,
                game_id=offer.game_id,
                event_id=offer.event_id,
                family=offer.family,
                reason=reason,
                market_id=offer.market_id,
                specifier=offer.specifier,
            )
        )

    def reject_leg(self, leg: Leg, reason: Reason) -> None:
        self.rejections.append(
            Rejection(
                league=leg.league,
                game_id=leg.game_id,
                event_id=leg.event_id,
                family=leg.family,
                reason=reason,
                market_id=leg.market_id,
                specifier=leg.specifier,
                side=leg.side,
                p_calibrated=leg.p_calibrated,
                edge=leg.edge,
            )
        )

    def market(self, lines: list[Offer]) -> None:
        """All of one event's lines in one market, or for one team in a team total."""
        first = lines[0]
        reason = _market_reason(first, self.config, self.now)
        if reason is not None:
            self.reject(first, reason)
            return
        assert first.game_id is not None  # noqa: S101 - `_market_reason` ruled this out.
        prediction = self.predictions.get(first.game_id)
        if prediction is None or _period_for(prediction, first) is None:
            self.reject(first, Reason.MISSING_PRED)
            return
        if _injury_uncertain(prediction, first, self.config):
            self.reject(first, Reason.NBA_INJURY_UNCERTAIN)
            return
        bookable = [offer for offer in lines if offer.bookable]
        if not bookable:
            self.reject(first, Reason.NO_BOOKABLE_LINE)
            return
        chosen = bookable if self.config.allow_alt_lines else [main_line(bookable)]
        limit = timedelta(seconds=self.config.max_offer_age_seconds)
        for offer in chosen:
            if self.now - offer.fetched_at > limit:
                self.reject_line(offer, Reason.STALE_ODDS)
            else:
                self.line(offer, prediction)

    def line(self, offer: Offer, prediction: Prediction) -> None:
        period = _period_for(prediction, offer)
        assert period is not None  # noqa: S101 - `market` ruled this out.
        calibrator = self.config.calibrators[offer.league][offer.family]
        above, below = score_two_way(*_chances(period, offer), calibrator)
        one, two = offer.outcomes
        for outcome, other in ((one, two), (two, one)):
            side = above if outcome.side in FIRST_SIDES else below
            leg = self.leg(offer, prediction, outcome.side, outcome.outcome_id, outcome.odds,
                           other.odds, side)  # fmt: skip
            reason = self.leg_reason(leg, prediction)
            if reason is None:
                self.legs.append(leg)
            else:
                self.reject_leg(leg, reason)

    def leg(
        self,
        offer: Offer,
        prediction: Prediction,
        side: str,
        outcome_id: str,
        odds: float,
        other_odds: float,
        chance: SideProbability,
    ) -> Leg:
        fair = fair_probability(odds, other_odds)
        assert offer.game_id is not None  # noqa: S101 - `market` ruled this out.
        return Leg.model_validate(
            {
                "rank": 0,
                "league": offer.league,
                "game_id": offer.game_id,
                "event_id": offer.event_id,
                "family": offer.family,
                "market_id": offer.market_id,
                "specifier": offer.specifier,
                "line": offer.line,
                "team": offer.team,
                "side": side,
                "outcome_id": outcome_id,
                "odds": odds,
                "p_raw": chance.p_raw,
                "p_calibrated": chance.p_calibrated,
                "p_win": chance.p_win,
                "p_push": chance.p_push,
                "p_lose": chance.p_lose,
                "fair": fair,
                "edge": chance.p_calibrated - fair,
                "ev": expected_value(chance.p_win, chance.p_lose, odds),
                "tip_time": offer.tip_time,
                "predicted_mean": _predicted_mean(prediction, offer),
                "model_version": prediction.model_version,
                "feature_hash": prediction.feature_hash,
                "offer_fetched_at": offer.fetched_at,
            }
        )

    def leg_reason(self, leg: Leg, prediction: Prediction) -> Reason | None:
        market = self.config.markets[leg.league][leg.family]
        assert market.min_prob is not None and market.min_edge is not None  # noqa: S101
        if leg.p_calibrated < market.min_prob:
            return Reason.LOW_PROB
        if leg.edge < market.min_edge:
            return Reason.LOW_EDGE
        strict = market.min_prob + self.config.low_confidence_extra_prob
        if _thin_data(prediction, self.config) and leg.p_calibrated < strict:
            return Reason.LOW_CONFIDENCE
        return None

    def tickets(self) -> list[Ticket]:
        built: list[Ticket] = []
        for league in sorted(self.config.leagues):
            for family in FAMILIES:
                market = [leg for leg in self.legs if (leg.league, leg.family) == (league, family)]
                built += self.market_tickets(league, family, sorted(market, key=_rank_key))
        return built

    def market_tickets(self, league: str, family: str, ranked: list[Leg]) -> list[Ticket]:
        best: list[Leg] = []
        games: set[str] = set()
        for leg in ranked:
            if leg.game_id in games:  # D10: `ranked` is best first, so the best one stayed.
                self.reject_leg(leg, Reason.SAME_GAME)
            else:
                games.add(leg.game_id)
                best.append(leg)
        settings = self.config.leagues[league]
        for leg in best[settings.most_legs :]:
            self.reject_leg(leg, Reason.BELOW_CUTOFF)
        chosen = best[: settings.most_legs]
        size = settings.max_legs_per_ticket
        tickets: list[Ticket] = []
        for start in range(0, len(chosen), size):
            legs = chosen[start : start + size]
            if len(legs) < self.config.min_legs:
                for leg in legs:
                    self.reject_leg(leg, Reason.TICKET_TOO_SHORT)
                continue
            tickets.append(self.ticket(league, family, len(tickets) + 1, start, legs))
        return tickets

    def ticket(self, league: str, family: str, number: int, start: int, legs: list[Leg]) -> Ticket:
        ranked = tuple(
            leg.model_copy(update={"rank": start + index}) for index, leg in enumerate(legs, 1)
        )
        odds = math.prod(leg.odds for leg in ranked)
        chance = math.prod(leg.p_win for leg in ranked)
        market = self.config.markets[league][family]
        return Ticket.model_validate(
            {
                "league": league,
                "family": family,
                "ticket_no": number,
                "legs": ranked,
                "combined_odds": odds,
                "combined_prob": chance,
                "expected_value": chance * odds - 1.0,
                "selection_hash": selection_hash(ranked),
                "passed_gate": market.passed_gate,
                "edge_label": market.edge_label,
            }
        )


def selection_hash(legs: Iterable[Leg]) -> str:
    """Identifies what a ticket selects, whatever the order of its legs (docs/07 section 6.4)."""
    parts = sorted(
        f"{leg.event_id}|{leg.market_id}|{leg.specifier}|{leg.outcome_id}" for leg in legs
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _check_inputs(
    predictions: Sequence[Prediction], offers: Sequence[Offer], config: EngineConfig, now: datetime
) -> None:
    if now.tzinfo is None:
        raise ValueError("`now` needs a timezone: all times are UTC")
    seen_games: set[str] = set()
    for prediction in predictions:
        if prediction.game_id in seen_games:
            raise ValueError(f"Two predictions for game {prediction.game_id}")
        seen_games.add(prediction.game_id)
    seen_lines: set[tuple[str, str, str]] = set()
    for offer in offers:
        key = (offer.event_id, offer.market_id, offer.specifier)
        if key in seen_lines:
            raise ValueError(f"Two offers for {key}")
        seen_lines.add(key)
        market = config.markets[offer.league][offer.family]
        usable = market.enabled and market.min_prob is not None
        if usable and offer.family not in config.calibrators.get(offer.league, {}):
            raise ValueError(f"No calibrator for {offer.league} {offer.family}")


def build_tickets(
    predictions: Sequence[Prediction],
    offers: Sequence[Offer],
    config: EngineConfig,
    now: datetime,
) -> BuildResult:
    _check_inputs(predictions, offers, config, now)
    build = _Build({item.game_id: item for item in predictions}, config, now, [], [])
    markets: dict[tuple[str, str, str, str], list[Offer]] = {}
    for offer in offers:
        markets.setdefault(
            (offer.league, offer.event_id, offer.family, offer.team or ""), []
        ).append(offer)
    for key in sorted(markets):
        build.market(markets[key])
    tickets = build.tickets()
    rejections = sorted(
        build.rejections,
        key=lambda item: (
            item.league,
            item.family,
            item.event_id,
            item.market_id or "",
            item.specifier or "",
            item.side or "",
            item.reason.value,
        ),
    )
    return BuildResult(
        tickets=tuple(tickets), rejections=tuple(rejections), assumptions=(INDEPENDENCE,)
    )
