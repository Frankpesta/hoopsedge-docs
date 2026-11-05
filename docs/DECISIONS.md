# Decisions Log

Append-only. Each entry: date, decision, reason, who. Claude Code adds entries whenever reality differs from the docs.

## 2026-11-04 — Initial decisions (owner-approved plan)

| ID | Decision | Notes |
|----|----------|-------|
| D1 | Current-season inputs + decaying preseason prior | Owner emphasised current-season data; prior needed because NCAA started this week |
| D2 | Four markets (handicap, FT O/U, HT O/U, team O/U), separate codes each | Owner request |
| D3 | NCAA: top 100 legs per market → tickets of 20; applies to every market | Owner answer to Q6 |
| D4 | Drop legs below confidence threshold even if tickets are short | Owner answer to Q7 |
| D5 | Main line only by default; alternate lines behind a flag | Recommended default; owner can change in Settings |
| D6 | Auto daily runs (NCAA 15:00, NBA 19:30 Lagos) + Run now | Recommended default; times editable |
| D7 | NG proxy only if SportyBet blocks AWS; owner approves any paid proxy | Recommended default |
| D8–D11 | No user auth (shared server secret), continuous learning with champion/challenger, one leg per game per ticket, never place bets | Owner requirements |

## Pending (filled during build)

- Phase 0: SportyBet endpoints, market IDs, geo result, max selections per booking
- Phase 0: data-source choice for NCAA (ESPN vs CBBD); historical lines availability
- Phase 4: chosen distribution (normal vs empirical), tuned thresholds, market gates
