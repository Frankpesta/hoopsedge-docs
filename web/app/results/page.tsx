import Link from "next/link";

import { SuggestionCard } from "@/components/SuggestionCard";
import { Empty, PageTitle, Section, StatTile } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import { MARKET_NAMES, MODE_LABELS, percent, signedPercent } from "@/lib/format";
import { SPORTS, isSport } from "@/lib/types";
import type { Calibration, CalibrationRow, ConfigDocument, ConfigView, Sport } from "@/lib/types";

export const dynamic = "force-dynamic";

function groupTables(rows: CalibrationRow[]): [string, CalibrationRow[]][] {
  const groups = new Map<string, CalibrationRow[]>();
  for (const row of rows) {
    const mode = MODE_LABELS[row.mode] ?? row.mode;
    const name = `${MARKET_NAMES[row.market] ?? row.market}, ${mode.toLowerCase()}, same-level only ${row.same_level ? "on" : "off"}`;
    groups.set(name, [...(groups.get(name) ?? []), row]);
  }
  return [...groups.entries()];
}

function BucketTable({ rows }: { rows: CalibrationRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="num w-full text-sm">
        <thead className="text-text-2 text-left text-xs">
          <tr>
            <th className="py-1 pr-2 font-medium">Edge</th>
            <th className="py-1 pr-2 font-medium">Cross-level</th>
            <th className="py-1 pr-2 text-right font-medium">Legs</th>
            <th className="py-1 pr-2 text-right font-medium">Hit</th>
            <th className="py-1 pr-2 text-right font-medium">Implied</th>
            <th className="py-1 text-right font-medium">ROI</th>
          </tr>
        </thead>
        <tbody className="divide-line divide-y">
          {rows.map((row) => (
            <tr key={`${row.bucket}-${row.cross_level}`}>
              <td className="py-1.5 pr-2">{row.bucket}</td>
              <td className="py-1.5 pr-2">{row.cross_level ? "Yes" : "No"}</td>
              <td className="py-1.5 pr-2 text-right">{row.legs}</td>
              <td className="py-1.5 pr-2 text-right">{percent(row.hit_rate)}</td>
              <td className="py-1.5 pr-2 text-right">{percent(row.implied)}</td>
              <td className="py-1.5 text-right">{signedPercent(row.roi)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function ResultsPage({
  searchParams,
}: {
  searchParams: Promise<{ sport?: string }>;
}) {
  const { sport: asked } = await searchParams;
  const sport: Sport = isSport(asked) ? asked : "NCAAB";
  const [found, config] = await Promise.all([
    apiGetFresh<Calibration>(`/calibration/${sport}`),
    apiGetFresh<ConfigView>("/config"),
  ]);
  const document = config.document as unknown as ConfigDocument;
  const lineModes = document.sports[sport].line_mode;
  const tickets = found.tickets;
  return (
    <>
      <PageTitle
        title="Results"
        sub="Every finished game is graded at the last lines seen before kickoff, whether or not it cleared a threshold, so any threshold can be judged."
      />
      <nav aria-label="Sport" className="mb-4 flex gap-2">
        {SPORTS.map((name) => (
          <Link
            key={name}
            href={`/results?sport=${name}`}
            aria-current={name === sport ? "page" : undefined}
            className={`font-display min-h-11 rounded-md px-4 py-2 text-lg font-semibold uppercase ${
              name === sport ? "bg-surface-2 text-text" : "text-muted"
            }`}
          >
            {name}
          </Link>
        ))}
      </nav>
      <div className="grid grid-cols-2 gap-2">
        <StatTile
          label="Graded legs"
          value={found.graded_legs}
          note={`${found.slates} slates · ${found.pending_legs} waiting for a final`}
        />
        <StatTile
          label="Tickets"
          value={`${tickets.won ?? 0} won · ${tickets.lost ?? 0} lost`}
          note={`${tickets.pending ?? 0} waiting`}
        />
      </div>

      <Section title="Threshold suggestions">
        <p className="text-text-2 mb-3 text-sm">
          Hit rate is judged against the bookmaker-implied rate, and on flat-stake ROI (1 unit per
          leg). Suggestions are for same-level only{" "}
          {document.sports[sport].l5_same_level_only ? "on" : "off"}, the setting in use. Nothing
          changes until you apply one.
        </p>
        {found.graded_legs === 0 ? (
          <Empty title="Nothing graded yet">
            Grading runs every morning at 09:00, once the night&apos;s games have finished.
          </Empty>
        ) : (
          <div className="space-y-3">
            {found.suggestions.map((suggestion) => (
              <SuggestionCard
                key={`${suggestion.market}-${suggestion.mode}`}
                sport={sport}
                suggestion={suggestion}
                activeMode={
                  suggestion.market === "totals" ? lineModes.totals : lineModes.team_totals
                }
                version={config.version}
                minGraded={found.min_graded}
              />
            ))}
          </div>
        )}
      </Section>

      {found.tables.length > 0 && (
        <Section title="By edge and cross-level">
          <div className="space-y-2">
            {groupTables(found.tables).map(([name, rows]) => (
              <details key={name} className="bg-surface border-line rounded-lg border px-3">
                <summary className="min-h-11 cursor-pointer py-3 text-sm">{name}</summary>
                <div className="pb-3">
                  <BucketTable rows={rows} />
                </div>
              </details>
            ))}
          </div>
        </Section>
      )}
    </>
  );
}
