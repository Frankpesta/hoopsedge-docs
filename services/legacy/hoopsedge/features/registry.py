"""Every model feature, named and versioned (docs/04-ratings-features.md section 3.3).

Change `FEATURE_SET_VERSION` whenever a name is added or a definition changes. h = home, a = away.
"""

FEATURE_SET_VERSION = "v1"

FACTORS = ("efg", "tov", "orb", "ftr", "par")
SIDES = ("h", "a")


def _sided(*names: str) -> tuple[str, ...]:
    return tuple(f"{name}_{side}" for name in names for side in SIDES)


FEATURES: tuple[str, ...] = (
    # Strength.
    *_sided("adj_o", "adj_d", "adj_t", "elo"),
    "elo_diff",
    # Matchup, full game and first half.
    "exp_poss",
    *_sided("exp_ppp"),
    "exp_poss_1h",
    *_sided("exp_ppp_1h"),
    # Style: each side's four factors and 3PA rate, on offence and defence.
    *_sided(*[f"{factor}_o" for factor in FACTORS], *[f"{factor}_d" for factor in FACTORS]),
    *_sided("orb_matchup", "tov_matchup", "efg_matchup", "ftr_matchup"),
    # Confidence.
    *_sided("games", "prior_weight", "volatility"),
    # Situation.
    "neutral",
    "hca",
    *_sided("rest", "b2b", "games_7d", "travel_km"),
    "days_since_start",
    "postseason",
    # Form.
    *_sided("form"),
    # Availability, NBA only.
    *_sided("avail_missing", "avail_starters"),
    # Calendar.
    "month",
    "is_weekend",
)

# D15: there's no injury history, so V1 models don't train on these. They're still computed and
# stored, so the normal challenger process can bring them in once enough snapshots exist.
UNTRAINED_FEATURES: tuple[str, ...] = _sided("avail_missing", "avail_starters")
