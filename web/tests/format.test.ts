import { describe, expect, it } from "vitest";

import {
  DROP_REASONS,
  FLAG_LABELS,
  KIND_NAMES,
  countdown,
  diffSettings,
  easternDate,
  formatOdds,
  isValidDate,
  lagosDateTime,
  lagosTime,
  legPick,
  oneDecimal,
  oneInN,
  percent,
  points,
  reasonLabel,
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

describe("v2 words", () => {
  it("counts down to a kickoff, and says when it has started", () => {
    const now = new Date("2026-11-28T12:00:00Z");
    expect(countdown("2026-11-28T15:05:00Z", now)).toBe("in 3 h 05 m");
    expect(countdown("2026-11-28T12:12:00Z", now)).toBe("in 12 m");
    expect(countdown("2026-11-28T11:59:00Z", now)).toBe("started");
    expect(countdown("2026-12-01T12:00:00Z", now)).toBe("in 3 days");
    expect(countdown(null, now)).toBe("all started");
  });

  it("writes points with a real minus sign and one decimal", () => {
    expect(points(4.5)).toBe("+4.5");
    expect(points(-6.75)).toBe("−6.8");
    expect(points(null)).toBe("");
    expect(oneDecimal(53)).toBe("53.0");
  });

  it("names a leg by team, or Total for a game total", () => {
    const base = { home: "Delaware", away: "UConn", direction: "Over", booked_line: 23.5 };
    expect(legPick({ ...base, team: "home" })).toBe("Delaware Over 23.5");
    expect(legPick({ ...base, team: "away" })).toBe("UConn Over 23.5");
    expect(legPick({ ...base, team: null, booked_line: 47.5 })).toBe("Total Over 47.5");
  });

  it("explains every drop reason the engine gives, and shows an unknown one as itself", () => {
    for (const code of [
      "started",
      "inside_cutoff",
      "lean_gone",
      "team_edge_gone",
      "no_open_line",
      "no_line_for_mode",
      "beyond_top_games",
      "ticket_below_min_legs",
    ]) {
      expect(DROP_REASONS[code]).toBeTruthy();
    }
    expect(reasonLabel("something_new")).toBe("something_new");
  });

  it("names the ticket kinds, flags and statuses", () => {
    expect(KIND_NAMES.TOTALS_LEAN).toBe("Totals lean");
    expect(FLAG_LABELS.cross_level_game).toBe("Cross-level game");
    expect(statusLabel("dry_run")).toBe("Practice");
    expect(statusLabel("booked")).toBe("Booked");
  });
});
