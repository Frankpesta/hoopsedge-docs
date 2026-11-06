"use client";

import { useState, type ReactNode } from "react";

export type Row = readonly (string | number)[];

/** Every chart has a text summary and a table: a tooltip or colour alone never gates a value. */
export function ChartFrame({
  title,
  summary,
  legend,
  columns,
  rows,
  children,
}: {
  title: string;
  summary: string;
  legend?: readonly { label: string; color: string }[];
  columns: readonly string[];
  rows: readonly Row[];
  children: ReactNode;
}) {
  const [table, setTable] = useState(false);
  return (
    <figure className="bg-surface border-line rounded-lg border p-4">
      <div className="flex items-start justify-between gap-3">
        <figcaption className="font-semibold">{title}</figcaption>
        <button
          type="button"
          onClick={() => setTable(!table)}
          aria-pressed={table}
          className="text-text-2 min-h-8 shrink-0 text-sm underline"
        >
          {table ? "Chart" : "Table"}
        </button>
      </div>
      <p className="text-text-2 mt-1 text-sm">{summary}</p>
      {legend && legend.length > 1 && !table && (
        <ul className="text-text-2 mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {legend.map((item) => (
            <li key={item.label} className="flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-0.5 w-4 rounded"
                style={{ background: item.color }}
              />
              {item.label}
            </li>
          ))}
        </ul>
      )}
      {table ? (
        <div className="mt-3 overflow-x-auto">
          <table className="num w-full text-left text-sm">
            <thead className="text-muted text-xs">
              <tr>
                {columns.map((column) => (
                  <th key={column} scope="col" className="pr-4 pb-1 font-normal">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index} className="border-line border-t">
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="py-1 pr-4">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="mt-3" aria-hidden>
          {children}
        </div>
      )}
    </figure>
  );
}

export const AXIS_TICK = { fill: "var(--muted)", fontSize: 12 };
export const TOOLTIP_STYLE = {
  background: "var(--surface-2)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  color: "var(--text)",
  fontSize: 12,
};
