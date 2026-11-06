import { BucketChart, ReliabilityChart, RollingChart } from "@/components/charts/RecordCharts";
import { Empty, PageTitle, Section, StatTile } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { FAMILIES, FAMILY_NAMES, isValidDate, percent, signedPercent } from "@/lib/format";
import type { TrackRecord } from "@/lib/types";

type Params = { league?: string; market?: string; from?: string; to?: string };

function query(params: Params): string {
  const search = new URLSearchParams();
  if (params.league === "NBA" || params.league === "NCAA") search.set("league", params.league);
  if (params.market && (FAMILIES as readonly string[]).includes(params.market)) {
    search.set("market", params.market);
  }
  if (params.from && isValidDate(params.from)) search.set("from", params.from);
  if (params.to && isValidDate(params.to)) search.set("to", params.to);
  const text = search.toString();
  return text ? `?${text}` : "";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="text-text-2 flex flex-col gap-1 text-xs">
      {label}
      {children}
    </label>
  );
}

const INPUT = "bg-surface border-line text-text min-h-11 w-full rounded-md border px-2 text-sm";

export default async function TrackRecordPage({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  const record = await apiGet<TrackRecord>(`/track-record${query(params)}`);
  const settled = record.won + record.lost;
  const nearest = Object.entries(record.missed_by).find(([missed]) => missed === "1")?.[1] ?? 0;
  return (
    <>
      <PageTitle
        title="Track record"
        sub="Latest ticket versions only, so a re-run can't inflate these numbers."
      />
      <form className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Field label="League">
          <select name="league" defaultValue={params.league ?? ""} className={INPUT}>
            <option value="">All</option>
            <option>NBA</option>
            <option>NCAA</option>
          </select>
        </Field>
        <Field label="Market">
          <select name="market" defaultValue={params.market ?? ""} className={INPUT}>
            <option value="">All</option>
            {FAMILIES.map((family) => (
              <option key={family} value={family}>
                {FAMILY_NAMES[family]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="From">
          <input type="date" name="from" defaultValue={params.from} className={INPUT} />
        </Field>
        <Field label="To">
          <input type="date" name="to" defaultValue={params.to} className={INPUT} />
        </Field>
        <button
          type="submit"
          className="bg-amber text-on-amber col-span-2 min-h-11 self-end rounded-md px-4 text-sm font-semibold sm:col-span-1"
        >
          Apply
        </button>
      </form>

      {record.legs === 0 ? (
        <div className="mt-6">
          <Empty title="No settled legs yet">
            Legs appear here once their games finish and grading has run.
          </Empty>
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
            <StatTile
              label="Leg hit rate"
              value={percent(record.hit_rate)}
              note={`${record.won} of ${settled} settled. Needed: ${percent(record.breakeven_hit_rate)}`}
            />
            <StatTile
              label="Flat-stake return"
              value={signedPercent(record.flat_stake_roi)}
              note="Per leg, before any ticket"
            />
            <StatTile label="Push rate" value={percent(record.push_rate)} />
            <StatTile
              label="Calibration error"
              value={percent(record.calibration_error)}
              note="Gap between predicted and real"
            />
            <StatTile
              label="Tickets won"
              value={record.tickets.won ?? 0}
              note={`${record.tickets.lost ?? 0} lost, ${record.tickets.pending ?? 0} pending`}
            />
            <StatTile
              label="Nearest misses"
              value={nearest}
              note={`Lost by one leg. ${record.legs_won_on_lost} of ${record.legs_on_lost} legs won on lost tickets`}
            />
          </div>
          <Section title="Calibration">
            <div className="space-y-4">
              <ReliabilityChart buckets={record.buckets} />
              <BucketChart buckets={record.buckets} />
            </div>
          </Section>
          <Section title="Over time">
            <RollingChart daily={record.daily} breakeven={record.breakeven_hit_rate} />
          </Section>
        </>
      )}
    </>
  );
}
