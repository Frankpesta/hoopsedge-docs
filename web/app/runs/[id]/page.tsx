import Link from "next/link";
import { notFound } from "next/navigation";

import { TicketCard } from "@/components/TicketCard";
import { Card, Empty, PageTitle, Section, StatusChip } from "@/components/ui";
import { apiGetFresh, apiGetOptional } from "@/lib/api";
import { lagosDateTime, reasonLabel } from "@/lib/format";
import type { RunDetail, SystemView } from "@/lib/types";

export const dynamic = "force-dynamic";

const COUNTS: Record<string, string> = {
  events_listed: "Events SportyBet listed",
  games: "Games on this slate",
  eligible: "Games with enough history",
  unmatched: "Events left unmatched",
  failures: "Events skipped for a bad shape",
  other_slates: "Events on other dates",
  tickets: "Tickets",
};

type Unmatched = {
  event_id: string;
  reason: string;
  home: { name: string };
  away: { name: string };
};
type Failure = { event_id: string; reason: string };

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [run, system] = await Promise.all([
    apiGetOptional<RunDetail>(`/runs/${encodeURIComponent(id)}`, 0),
    apiGetFresh<SystemView>("/system"),
  ]);
  if (!run) notFound();
  const unmatched = run.unmatched_events as Unmatched[];
  const failures = run.failures as Failure[];
  return (
    <>
      <PageTitle
        title={`${run.sport} ${run.trigger === "rebook" ? "rebook" : "run"}`}
        sub={
          <>
            Slate{" "}
            <Link href={`/slate/${run.slate}?sport=${run.sport}`} className="underline">
              {run.slate}
            </Link>{" "}
            · {lagosDateTime(run.started_at)} · {run.trigger}
            {run.dry_run ? " · practice codes" : " · real codes"}
          </>
        }
      />
      <StatusChip status={run.status} />
      {run.error && <p className="text-loss mt-2 text-sm">{run.error}</p>}

      {Object.keys(run.counts).length > 0 && (
        <Section title="Counts">
          <Card>
            <ul className="space-y-1 text-sm">
              {Object.entries(run.counts).map(([name, value]) => (
                <li key={name} className="flex justify-between gap-3">
                  <span className="text-text-2">{COUNTS[name] ?? name}</span>
                  <span className="num">{value}</span>
                </li>
              ))}
            </ul>
          </Card>
        </Section>
      )}

      <Section title="Tickets">
        {run.tickets_detail.length ? (
          <div className="space-y-4">
            {run.tickets_detail.map((ticket) => (
              <TicketCard key={ticket.ticket_id} ticket={ticket} realCodes={!system.dry_run} />
            ))}
          </div>
        ) : (
          <Empty title="No tickets">
            No ticket kind had enough legs that cleared their thresholds.
          </Empty>
        )}
      </Section>

      {Object.keys(run.dropped).length > 0 && (
        <Section title="Legs left out">
          <Card>
            <ul className="space-y-1 text-sm">
              {Object.entries(run.dropped).map(([reason, count]) => (
                <li key={reason} className="flex justify-between gap-3">
                  <span className="text-text-2">{reasonLabel(reason)}</span>
                  <span className="num">{count}</span>
                </li>
              ))}
            </ul>
          </Card>
        </Section>
      )}

      {(unmatched.length > 0 || failures.length > 0) && (
        <Section title="Events not used">
          <Card>
            <ul className="space-y-2 text-sm">
              {unmatched.map((item) => (
                <li key={item.event_id}>
                  {item.away.name} at {item.home.name}
                  <span className="text-text-2 block text-xs">Unmatched: {item.reason}</span>
                </li>
              ))}
              {failures.map((item) => (
                <li key={item.event_id}>
                  {item.event_id}
                  <span className="text-text-2 block text-xs">{item.reason}</span>
                </li>
              ))}
            </ul>
          </Card>
        </Section>
      )}
    </>
  );
}
