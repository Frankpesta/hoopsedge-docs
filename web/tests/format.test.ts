import { describe, expect, it } from "vitest";

import {
  diffSettings,
  easternDate,
  formatOdds,
  isValidDate,
  lagosDateTime,
  lagosTime,
  normalBelow,
  oneInN,
  outcomeLabel,
  percent,
  reasonLabel,
  REASONS,
  rollingHitRate,
  secondsBetween,
  shiftDate,
  signedNumber,
  signedPercent,
  statusLabel,
} from "@/lib/format";

describe("oneInN", () => {
  it.each([
    [0.5, "~1 in 2"],
    [0.25, "~1 in 4"],
    [1 / 3812, "~1 in 3,800"],
    [1 / 123456, "~1 in 120,000"],
    [0.0757, "~1 in 13"],
  ])("rounds %f to %s, claiming no false precision", (value, expected) => {
    expect(oneInN(value)).toBe(expected);
  });

  it("says nothing rather than inventing a figure", () => {
    expect(oneInN(null)).toBeNull();
    expect(oneInN(undefined)).toBeNull();
    expect(oneInN(0)).toBeNull();
  });
});

describe("odds and percentages", () => {
  it("formats odds", () => {
    expect(formatOdds(1.9)).toBe("1.90");
    expect(formatOdds(229631.7)).toBe("229,632");
  });

  it("formats chances, and missing ones as n/a", () => {
    expect(percent(0.5236)).toBe("52.4%");
    expect(percent(null)).toBe("n/a");
    expect(signedPercent(0.012)).toBe("+1.2%");
    expect(signedPercent(-0.012)).toBe("−1.2%");
    expect(signedPercent(undefined)).toBe("n/a");
    expect(signedNumber(-5, 1)).toBe("−5.0");
    expect(signedNumber(5)).toBe("+5.00");
  });
});

describe("times", () => {
  it("shows Lagos time, a real tip at 01:00 UTC being 02:00 there", () => {
    expect(lagosTime("2026-11-07T01:00:00+00:00")).toBe("Sat 02:00");
    expect(lagosDateTime("2026-11-07T01:00:00+00:00")).toBe("7 Nov 02:00");
  });

  it("copes with no time or a bad one", () => {
    expect(lagosTime(null)).toBe("n/a");
    expect(lagosTime("nonsense")).toBe("n/a");
    expect(lagosDateTime(undefined)).toBe("n/a");
  });

  it("names a slate by its Eastern date, which lags Lagos in the small hours", () => {
    expect(easternDate(new Date("2026-11-07T03:00:00Z"))).toBe("2026-11-06");
    expect(easternDate(new Date("2026-11-07T12:00:00Z"))).toBe("2026-11-07");
  });

  it("moves dates across a month end", () => {
    expect(shiftDate("2026-11-30", 1)).toBe("2026-12-01");
    expect(shiftDate("2026-12-01", -1)).toBe("2026-11-30");
  });

  it("checks dates", () => {
    expect(isValidDate("2026-11-06")).toBe(true);
    expect(isValidDate("yesterday")).toBe(false);
    expect(isValidDate("2026-13-45")).toBe(false);
  });

  it("measures step lengths, and a step still going has none", () => {
    expect(secondsBetween("2026-11-06T10:00:00Z", "2026-11-06T10:00:30Z")).toBe(30);
    expect(secondsBetween("2026-11-06T10:00:00Z", null)).toBeNull();
  });
});

describe("rolling hit rate", () => {
  it("counts only the trailing 14 days and only settled legs", () => {
    const series = rollingHitRate([
      { date: "2026-11-01", won: 1, lost: 0 },
      { date: "2026-11-14", won: 0, lost: 1 },
      { date: "2026-11-15", won: 0, lost: 1 },
    ]);
    expect(series[1]).toEqual({ date: "2026-11-14", rate: 0.5, settled: 2 });
    // The 1st is now out of the window.
    expect(series[2]).toEqual({ date: "2026-11-15", rate: 0, settled: 2 });
  });

  it("skips days with nothing settled yet", () => {
    expect(rollingHitRate([{ date: "2026-11-01", won: 0, lost: 0 }])).toEqual([]);
  });
});

describe("the normal curve", () => {
  it("is right at the landmarks", () => {
    expect(normalBelow(0, 0, 1)).toBeCloseTo(0.5, 5);
    expect(normalBelow(1.96, 0, 1)).toBeCloseTo(0.975, 3);
    expect(normalBelow(-1.96, 0, 1)).toBeCloseTo(0.025, 3);
    expect(normalBelow(110, 100, 10)).toBeCloseTo(0.8413, 3);
  });
});

describe("outcome labels on the game page", () => {
  const lines = [
    { market_id: "223", specifier: "hcp=-20.5", line: -20.5, team: null },
    { market_id: "227", specifier: "total=89.5", line: 89.5, team: "home" },
    { market_id: "228", specifier: "total=68.5", line: 68.5, team: "away" },
    { market_id: "225", specifier: "total=157.5", line: 157.5, team: null },
    { market_id: "68", specifier: "total=75.5", line: 75.5, team: null },
  ];
  const label = (row: Record<string, unknown>) =>
    outcomeLabel(row, lines, "Oregon St", "Cal Baptist");

  it("gives each handicap side its own sign", () => {
    const base = { family: "handicap", market_id: "223", specifier: "hcp=-20.5" };
    expect(label({ ...base, side: "home" })).toBe("Oregon St -20.5");
    expect(label({ ...base, side: "away" })).toBe("Cal Baptist +20.5");
  });

  it("trusts the canonical line, not SportyBet's specifier, on a swapped game", () => {
    // SportyBet listed these the other way round, so its specifier has the opposite sign.
    const swapped = [{ market_id: "223", specifier: "hcp=-3.5", line: 3.5, team: null }];
    const row = { family: "handicap", market_id: "223", specifier: "hcp=-3.5", side: "home" };
    expect(outcomeLabel(row, swapped, "Michigan", "UConn")).toBe("Michigan +3.5");
  });

  it("names the team on a team total, even when the row itself doesn't say", () => {
    expect(
      label({ family: "team_total", market_id: "227", specifier: "total=89.5", side: "under" }),
    ).toBe("Oregon St Under 89.5");
    expect(
      label({ family: "team_total", market_id: "228", specifier: "total=68.5", side: "over" }),
    ).toBe("Cal Baptist Over 68.5");
  });

  it("names totals plainly", () => {
    expect(
      label({ family: "total_ft", market_id: "225", specifier: "total=157.5", side: "over" }),
    ).toBe("Full game Over 157.5");
    expect(
      label({ family: "total_1h", market_id: "68", specifier: "total=75.5", side: "under" }),
    ).toBe("1H Under 75.5");
  });

  it("uses a leg's own line and team when it has them", () => {
    expect(label({ family: "team_total", team: "away", line: 70.5, side: "over" })).toBe(
      "Cal Baptist Over 70.5",
    );
  });

  it("names a whole market rejected before any line was looked at", () => {
    expect(label({ family: "handicap" })).toBe("Handicap");
    expect(label({ family: "total_ft" })).toBe("Full game total");
    expect(label({ family: "total_1h" })).toBe("1H total");
    expect(label({ family: "team_total" })).toBe("Team total");
  });

  it("never prints a line it can't place", () => {
    expect(label({ family: "handicap", side: "away", specifier: "hcp=-9.5" })).toBe(
      "Cal Baptist handicap",
    );
  });
});

describe("settings preview", () => {
  it("names each change by its full path", () => {
    expect(diffSettings({ a: { b: 1, c: 2 } }, { a: { b: 1, c: 3 } })).toEqual([
      { path: "a.c", from: 2, to: 3 },
    ]);
  });

  it("finds nothing when nothing changed, and handles clearing a threshold", () => {
    expect(diffSettings({ a: 1 }, { a: 1 })).toEqual([]);
    expect(diffSettings({ min_prob: 0.5 }, { min_prob: null })).toEqual([
      { path: "min_prob", from: 0.5, to: null },
    ]);
  });
});

describe("labels", () => {
  it("explains every reason code the engine can give", () => {
    for (const code of [
      "UNMATCHED",
      "STARTED",
      "TOO_CLOSE",
      "MARKET_DISABLED",
      "NO_THRESHOLD",
      "NO_BOOKABLE_LINE",
      "STALE_ODDS",
      "MISSING_PRED",
      "NBA_INJURY_UNCERTAIN",
      "LOW_PROB",
      "LOW_EDGE",
      "LOW_CONFIDENCE",
      "SAME_GAME",
      "BELOW_CUTOFF",
      "TICKET_TOO_SHORT",
    ]) {
      // prettier-ignore
      expect(REASONS[code], code).toBeTruthy();
    }
  });

  it("shows an unknown code as itself rather than hiding it", () => {
    expect(reasonLabel("NEW_CODE")).toBe("NEW_CODE");
    expect(statusLabel("new_status")).toBe("new_status");
    expect(statusLabel("dry_run")).toBe("Dry run");
  });
});
