# Phase 0: SportyBet Discovery

Run on 2026-11-05 at about 15:40 UTC, from the owner's machine in Docker, with `scripts/sportybet_discovery.py`. Raw numbers are in `phase0-sportybet-discovery.json`, and test codes are in `phase0-sportybet-test-codes.json`. IDs and URLs are in `services/hoopsedge/sportybet/markets.yaml` and `endpoints.yaml`.

**Result:** booking and verification are feasible from this machine. Every read and booking worked with no login, cookie or token, and no JS challenge appeared. Access from AWS is still untested (D22).

**Main limit:** this is one snapshot of one early-season day. Coverage figures may differ on other days and at other times of day.

## Endpoints

All paths are under `https://www.sportybet.com/api/ng`. HTTP 200 doesn't mean success, so always check `bizCode` (10000 is success).

| Purpose | Call | Notes |
|---------|------|-------|
| Upcoming events | `GET /factsCenter/pcUpcomingEvents` | Params: `sportId`, `marketId` (comma-separated), `tournamentId`, `pageSize` (100 works), `pageNum` from 1. `data.totalNum` is the event total. |
| Event detail | `GET /factsCenter/event` | Params: `eventId`, `productId=3`. Not needed for the four families (see below). |
| Live events | `GET /factsCenter/liveOrPrematchEvents` | Param: `sportId`. Used only to record status values. |
| Create code | `POST /orders/share` | Body `{"selections":[{eventId, marketId, specifier, outcomeId}]}`, headers `Content-Type` and `OperId: 2`. The code is at `data.shareCode`. |
| Decode code | `GET /orders/share/{code}` | Each event in `data.outcomes` holds the selected market and outcome. `data.unavailableOutcomes` and `data.ticket.selections` are also returned. |

The site's own JavaScript confirms the create and decode calls. Codes expire: `data.deadline` was 10 days after the last leg's tip on all 4 codes.

## Markets

| Family | Market ID | Name on site | Specifier | Outcomes | Overtime |
|--------|-----------|--------------|-----------|----------|----------|
| `handicap` | 223 | Handicap (incl. overtime) | `hcp=-1.5` (home handicap) | 1714 home, 1715 away | Included |
| `total_ft` | 225 | Over/Under (incl. overtime) | `total=140.5` | 12 over, 13 under | Included |
| `total_1h` | 68 | 1st half - total | `total=63.5` | 12 over, 13 under | Not applicable |
| `team_total` | 227 and 228 | Home / Away O/U (incl. overtime) | `total=67.5` | 12 over, 13 under | Included |

- **Tournaments:** NBA is `sr:tournament:132`, NCAA is `sr:tournament:648` ("NCAA, Regular Season"). Postseason NCAA IDs aren't listed yet.
- **Overtime:** this comes from SportyBet's market text. No settlement has been observed.
- **A trap to avoid:** market 18, also called "Over/Under", covers regular time only. It isn't interchangeable with 225.
- **Handicap sign:** all 406 listed handicap lines match the home-handicap reading.
- **Half-point lines:** all 2,199 lines listed in these five markets end in .5, so none could push at this snapshot. Phase 5 should still handle whole numbers.

## Coverage on the first slate date (2026-11-05 ET)

| League | ESPN games | Listed | 223 | 225 | 68 | 227 | 228 | All five bookable |
|--------|-----------|--------|-----|-----|-----|-----|-----|------|
| NBA | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 2 |
| NCAA | 65 | 43 | 43 | 43 | 42 | 40 | 40 | 40 |

Columns 223 to 228 count events with at least one bookable line.

- **NCAA volume:** 43 of ESPN's 65 games were listed. Listed games weren't matched to ESPN one by one, so which games are missing isn't known. Listings may also grow later in the day.
- **Next-day listings:** only 1 NCAA game for 2026-11-06 was listed at this time.
- **Far-future games:** 5 NBA Christmas games were listed, with only the full-game total bookable. Their handicap and team-total lines were all locked, and they had no first-half lines.

## Main line

The rule in `discovery.py`: among bookable lines, choose the most even prices. A tie goes to SportyBet's `favourite` flag, then to the first specifier.

Each event has one `favourite` line per market. On the first slate date, the rule chose it on 217 of 218 market checks. The exception was a suspended favourite, which shows the flag can't be used alone.

## Bulk fetching

One list call per tournament returns every line of the five markets. On the 3 events compared, the list held every line the detail call had. It also held extra lines (5, 8 and 20) that the detail call left out, so market `status` must be checked. Three events is a small sample.

## Booking tests

4 of the 5 permitted codes were used. All were created with one POST and no cookies, and all decoded to an exact match.

| Probe | Legs | Result |
|-------|------|--------|
| `two-leg` | 2 NBA | `M7SVLQ`, verified |
| `all-markets` | 5 NCAA, one per market ID | `NSE2L4`, verified |
| `twenty-legs` | 20 NCAA, 20 events | `Q8L2JC`, verified |
| `same-event` | 2 NCAA, same event | `V04MC9`, verified. Same-event legs are **accepted**. |
| `bad-outcome` | 1 valid, 1 fake outcome ID | Rejected: `bizCode` 19000, "invalid outcome data". No code. |

- **Selection limit:** 20 legs work. The site's config says 50, which wasn't tested because tickets never exceed 20.
- **Rejections don't name a leg.** The whole request failed and the response had no `data`. Only a fake outcome ID was tried. Suspended lines and changed odds weren't tested, and the same `bizCode` also means an invalid code on lookup.

## Status values

| Field | Value | Meaning | Evidence |
|-------|-------|---------|----------|
| Event `status` | 0 | "Not start" | Observed |
| Event `status` | 1 | Live (Q1, Q2, Q4) | Observed |
| Event `status` | 2 | Grouped with live | Site JavaScript only |
| Event `status` | 3 to 6, and 7 | Finished, and interrupted | Site JavaScript only |
| Market `status` | 0 | Open | Observed |
| Market `status` | 2 | Locked | Observed. The site's UI locks any non-zero value. |

Suspended and postponed events weren't seen, so their values are unconfirmed. Treating anything except status 0 as not bookable is safe either way. `bookingStatus` ("Booked" or "Unavailable") was recorded, but its meaning isn't known.

## Home and away

SportyBet's home team matched ESPN's on both NBA games tonight. Neutral-site games weren't checked.

## Requests

The read pass made 10 SportyBet requests at 1 per second, all returning 200 with no retries (median latency 0.34 s). The site's `robots.txt` doesn't disallow these API paths.

## Definition of Done

| Item | Status |
|------|--------|
| `markets.yaml` with verified IDs, four families, both leagues | Done |
| Fixtures for events, detail, booking and decode (3+ each) | Done, in `services/tests/fixtures/sportybet/` |
| Geo decision | **Open:** owner's machine passes, AWS is deferred to deployment (D20, D22) |
| Owner review of DECISIONS entries | **Open:** D30 to D35 |
