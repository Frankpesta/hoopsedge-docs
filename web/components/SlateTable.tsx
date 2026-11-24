"use client";

import { type ReactNode, useMemo, useState } from "react";

import { FLAG_LABELS, formatOdds, lagosTime, oneDecimal, points } from "@/lib/format";
import type { SlateRow } from "@/lib/types";

import { FlagBadge } from "./ui";

type Column = {
  key: string;
  label: string;
  sort?: (row: SlateRow) => number | string;
  cell: (row: SlateRow) => ReactNode;
  className?: string;
};

const tiers = (pair: readonly string[]) => pair.join("/");
const l5 = (side: SlateRow["home_l5"]) => `${side.pf.toFixed(1)}/${side.pa.toFixed(1)}`;

/** The owner's sheet (brief §8.1), with the v2 columns after it. */
const COLUMNS: Column[] = [
  {
    key: "kickoff",
    label: "Kickoff (WAT)",
    sort: (r) => r.kickoff,
    cell: (r) => lagosTime(r.kickoff),
  },
  {
    key: "game",
    label: "Away @ Home",
    sort: (r) => `${r.away} ${r.home}`,
    cell: (r) => (
      <span className="font-medium whitespace-nowrap">
        {r.away} <span className="text-text-2">@</span> {r.home}
      </span>
    ),
    className: "sticky left-0 bg-surface",
  },
  {
    key: "tiers",
    label: "Atk/Def (A · H)",
    cell: (r) => `${tiers(r.away_tiers)} · ${tiers(r.home_tiers)}`,
  },
  { key: "l5", label: "L5 PF/PA (A · H)", cell: (r) => `${l5(r.away_l5)} · ${l5(r.home_l5)}` },
  {
    key: "sum",
    label: "Sum of avgs",
    sort: (r) => r.sum_of_avgs,
    cell: (r) => oneDecimal(r.sum_of_avgs),
  },
  {
    key: "model",
    label: "Model total",
    sort: (r) => r.model_total,
    cell: (r) => oneDecimal(r.model_total),
  },
  { key: "outlook", label: "Outlook", sort: (r) => r.outlook, cell: (r) => r.outlook },
  {
    key: "line",
    label: "Main line",
    sort: (r) => r.main_line ?? -1,
    cell: (r) => oneDecimal(r.main_line),
  },
  {
    key: "lean",
    label: "Lean",
    sort: (r) => r.lean ?? (r.eligible ? "" : "SKIP"),
    cell: (r) =>
      r.lean && r.lean !== "PASS" ? (
        <span className="text-amber font-semibold">{r.lean}</span>
      ) : (
        <span className="text-text-2">{r.lean ?? (r.eligible ? "No line" : "Skip")}</span>
      ),
  },
  {
    key: "edge",
    label: "Edge vs main",
    sort: (r) => Math.abs(r.edge_main ?? 0),
    cell: (r) => points(r.edge_main),
  },
  {
    key: "booked",
    label: "Booked",
    sort: (r) => r.booked_line ?? -1,
    cell: (r) =>
      r.booked_line === null ? "" : `${r.booked_line} @ ${formatOdds(r.booked_odds ?? 0)}`,
  },
  { key: "edge_booked", label: "Edge vs booked", cell: (r) => points(r.edge_booked) },
  {
    key: "proj",
    label: "Proj (A · H)",
    cell: (r) => `${oneDecimal(r.away_proj)} · ${oneDecimal(r.home_proj)}`,
  },
  {
    key: "team",
    label: "Team pick",
    sort: (r) => Math.abs(r.team_pick?.edge ?? 0),
    cell: (r) =>
      r.team_pick
        ? `${r.team_pick.team} ${r.team_pick.direction} ${r.team_pick.line}${
            r.team_pick.odds ? ` @ ${formatOdds(r.team_pick.odds)}` : ""
          }`
        : "",
  },
  {
    key: "flags",
    label: "Flags",
    sort: (r) => r.flags.length,
    cell: (r) => (
      <span className="flex flex-wrap gap-1">
        {r.flags.map((flag) => (
          <FlagBadge key={flag} label={FLAG_LABELS[flag] ?? flag} />
        ))}
        {!r.eligible && <FlagBadge label={FLAG_LABELS[r.skip_reason ?? ""] ?? "Skipped"} />}
      </span>
    ),
  },
];

export function SlateTable({ rows }: { rows: readonly SlateRow[] }) {
  const [sortKey, setSortKey] = useState("kickoff");
  const [ascending, setAscending] = useState(true);
  const sorted = useMemo(() => {
    const column = COLUMNS.find((item) => item.key === sortKey);
    if (!column?.sort) return rows;
    const by = column.sort;
    return [...rows].sort((a, b) => {
      const left = by(a);
      const right = by(b);
      const order = left < right ? -1 : left > right ? 1 : 0;
      return ascending ? order : -order;
    });
  }, [rows, sortKey, ascending]);

  function sortBy(key: string) {
    if (key === sortKey) setAscending(!ascending);
    else {
      setSortKey(key);
      setAscending(key === "kickoff" || key === "game");
    }
  }

  return (
    <div className="border-line overflow-x-auto rounded-lg border">
      <table className="w-full text-left text-xs">
        <thead className="bg-surface-2 text-text-2">
          <tr>
            {COLUMNS.map((column) => (
              <th
                key={column.key}
                scope="col"
                aria-sort={
                  sortKey === column.key ? (ascending ? "ascending" : "descending") : undefined
                }
                className={`px-2 py-2 font-medium whitespace-nowrap ${column.className ?? ""} ${
                  column.className ? "bg-surface-2" : ""
                }`}
              >
                {column.sort ? (
                  <button type="button" onClick={() => sortBy(column.key)} className="min-h-8">
                    {column.label}
                    {sortKey === column.key ? (ascending ? " ▲" : " ▼") : ""}
                  </button>
                ) : (
                  column.label
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-line divide-y">
          {sorted.map((row) => (
            <tr
              key={row.event_id}
              className={row.started ? "text-muted" : ""}
              title={row.started ? "Started: no longer bookable" : undefined}
            >
              {COLUMNS.map((column) => (
                <td
                  key={column.key}
                  className={`num px-2 py-2 whitespace-nowrap ${column.className ?? ""}`}
                >
                  {column.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
