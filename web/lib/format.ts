/** Display rules: times show in Lagos, slates are named by their US Eastern date. */

export const FAMILY_NAMES: Record<string, string> = {
  handicap: "Handicap",
  total_ft: "FT O/U",
  total_1h: "HT O/U",
  team_total: "Team O/U",
};

export const FAMILIES = ["handicap", "total_ft", "total_1h", "team_total"] as const;

export const REASONS: Record<string, string> = {
  UNMATCHED: "Couldn't be matched to a game",
  STARTED: "Game already started",
  TOO_CLOSE: "Too close to tip-off",
  MARKET_DISABLED: "Market switched off",
  NO_THRESHOLD: "No threshold exists for this market",
  NO_BOOKABLE_LINE: "SportyBet had no bookable line",
  STALE_ODDS: "Odds went stale",
  MISSING_PRED: "No prediction for this game",
  NBA_INJURY_UNCERTAIN: "Questionable starter, and injury news too old",
  LOW_PROB: "Probability below the threshold",
  LOW_EDGE: "Edge below the threshold",
  LOW_CONFIDENCE: "Too little data on a team this early",
  SAME_GAME: "Another pick on this game was stronger",
  BELOW_CUTOFF: "Past the top legs for this market",
  TICKET_TOO_SHORT: "Too few legs to make a ticket",
  MODEL_DISAGREES: "Model too far from the bookmaker's line to trust",
  THIN_DATA_CUTOFF: "Past the early-season limit for this market",
};

const STATUS: Record<string, string> = {
  verified: "Verified",
  dry_run: "Dry run",
  invalid: "Invalid",
  failed: "Failed",
  superseded: "Superseded",
  pending_booking: "Booking",
  succeeded: "Succeeded",
  running: "Running",
  pending: "Pending",
  won: "Won",
  lost: "Lost",
  push: "Push",
  void: "Void",
};

export function statusLabel(status: string): string {
  return STATUS[status] ?? status;
}

export function reasonLabel(code: string): string {
  return REASONS[code] ?? code;
}

export function formatOdds(odds: number): string {
  if (odds >= 1000) return Math.round(odds).toLocaleString("en-US");
  return odds.toFixed(2);
}

/** "~1 in 3,800": two significant figures, because more would claim false precision. */
export function oneInN(probability: number | null | undefined): string | null {
  if (probability === null || probability === undefined || probability <= 0) return null;
  if (probability >= 1) return "~1 in 1";
  const n = 1 / probability;
  const magnitude = 10 ** Math.max(0, Math.floor(Math.log10(n)) - 1);
  return `~1 in ${(Math.round(n / magnitude) * magnitude).toLocaleString("en-US")}`;
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "n/a";
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "n/a";
  const shown = (value * 100).toFixed(digits);
  return `${value >= 0 ? "+" : "−"}${shown.replace("-", "")}%`;
}

export function signedNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "n/a";
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

const lagosTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});

const lagosDateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export function lagosTime(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "n/a" : lagosTimeFormat.format(when).replace(",", "");
}

export function lagosDateTime(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "n/a" : lagosDateTimeFormat.format(when).replace(",", "");
}

export function easternDate(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function shiftDate(date: string, days: number): string {
  const when = new Date(`${date}T12:00:00Z`);
  when.setUTCDate(when.getUTCDate() + days);
  return when.toISOString().slice(0, 10);
}

export function longDate(date: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date(`${date}T12:00:00Z`));
}

export function isValidDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(new Date(`${value}T12:00:00Z`).getTime())
  );
}

type OutcomeRow = Record<string, unknown>;

/**
 * One scored outcome in words. It uses the canonical line, never SportyBet's specifier: that's on
 * SportyBet's home team, so on a swapped neutral game its sign would be backwards, and even on a
 * normal game it's the wrong sign for the away side.
 */
export function outcomeLabel(
  row: OutcomeRow,
  lines: readonly OutcomeRow[],
  home: string,
  away: string,
): string {
  const family = String(row.family);
  const market = lines.find(
    (item) => item.market_id === row.market_id && item.specifier === row.specifier,
  );
  const line = typeof row.line === "number" ? row.line : market?.line;
  const team = row.team ?? market?.team;
  const side = row.side ? String(row.side) : null;
  const direction = side ? side.charAt(0).toUpperCase() + side.slice(1) : "";
  const known = typeof line === "number";
  if (family === "handicap") {
    if (!side) return "Handicap";
    const name = side === "home" ? home : away;
    if (!known) return `${name} handicap`;
    return `${name} ${(side === "home" ? line : -line) >= 0 ? "+" : ""}${side === "home" ? line : -line}`;
  }
  const number = known ? ` ${line}` : "";
  if (family === "team_total") {
    const name = team === "home" ? `${home} ` : team === "away" ? `${away} ` : "Team ";
    return side ? `${name}${direction}${number}` : `${name.trim()} total`;
  }
  const prefix = family === "total_1h" ? "1H" : "Full game";
  return side ? `${prefix} ${direction}${number}` : `${prefix} total`;
}

/** Exact paths and values: docs/10 wants every setting change previewed before it's saved. */
export function diffSettings(
  before: unknown,
  after: unknown,
  path = "",
): { path: string; from: unknown; to: unknown }[] {
  if (before && after && typeof before === "object" && typeof after === "object") {
    const left = before as Record<string, unknown>;
    const right = after as Record<string, unknown>;
    return [...new Set([...Object.keys(left), ...Object.keys(right)])]
      .sort()
      .flatMap((key) => diffSettings(left[key], right[key], path ? `${path}.${key}` : key));
  }
  return before === after ? [] : [{ path, from: before, to: after }];
}

/** Seconds between two timestamps, for run steps. */
export function secondsBetween(start: string, end: string | null | undefined): number | null {
  if (!end) return null;
  const seconds = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  return Number.isFinite(seconds) ? seconds : null;
}

/** A trailing hit rate per day, from settled legs only. */
export function rollingHitRate(
  daily: readonly { date: string; won: number; lost: number }[],
  days = 14,
): { date: string; rate: number; settled: number }[] {
  const sorted = [...daily].sort((a, b) => a.date.localeCompare(b.date));
  return sorted.flatMap((point) => {
    const first = shiftDate(point.date, -(days - 1));
    let won = 0;
    let settled = 0;
    for (const other of sorted) {
      if (other.date >= first && other.date <= point.date) {
        won += other.won;
        settled += other.won + other.lost;
      }
    }
    return settled ? [{ date: point.date, rate: won / settled, settled }] : [];
  });
}

/** The normal curve, and chances either side of a line, for the game page. */
export function normalDensity(x: number, mean: number, sd: number): number {
  const z = (x - mean) / sd;
  return Math.exp(-0.5 * z * z) / (sd * Math.sqrt(2 * Math.PI));
}

export function normalBelow(x: number, mean: number, sd: number): number {
  const z = (x - mean) / sd;
  const t = 1 / (1 + 0.2316419 * Math.abs(z));
  const d = 0.3989423 * Math.exp((-z * z) / 2);
  const tail =
    d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
  return z > 0 ? 1 - tail : tail;
}
