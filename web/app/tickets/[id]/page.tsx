import Link from "next/link";
import { notFound } from "next/navigation";

import { Countdown } from "@/components/Countdown";
import { RebookButton } from "@/components/RebookButton";
import { TicketStub } from "@/components/TicketStub";
import { Card, FlagBadge, PageTitle, Section, StatusChip } from "@/components/ui";
import { apiGetFresh, apiGetOptional } from "@/lib/api";
import {
  FLAG_LABELS,
  KIND_NAMES,
  MODE_LABELS,
  formatOdds,
  lagosDateTime,
  legPick,
  oneInN,
  percent,
  points,
} from "@/lib/format";
import type { SystemView, TicketView } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [ticket, system] = await Promise.all([
    apiGetOptional<TicketView>(`/tickets/${encodeURIComponent(id)}`, 0),
    apiGetFresh<SystemView>("/system"),
  ]);
  if (!ticket) notFound();
  return (
    <>
      <PageTitle
        title={`${KIND_NAMES[ticket.kind] ?? ticket.kind} #${ticket.number}`}
        sub={
          <>
            {ticket.sport} · slate{" "}
            <Link href={`/slate/${ticket.slate}?sport=${ticket.sport}`} className="underline">
              {ticket.slate}
            </Link>{" "}
            · run{" "}
            <Link href={`/runs/${ticket.run_id}`} className="underline">
              {ticket.run_id.slice(-6)}
            </Link>
          </>
        }
      />
      <div className="mb-3 flex items-center justify-between gap-2">
        <StatusChip status={ticket.status} />
        {ticket.current && ticket.status !== "invalid" && (
          <RebookButton
            ticketId={ticket.ticket_id}
            startedLegs={ticket.started_legs}
            realCodes={!system.dry_run}
          />
        )}
      </div>
      <TicketStub code={ticket.code ?? null} status={ticket.status} />
      {ticket.reason && <p className="text-loss mt-2 text-sm">{ticket.reason}</p>}

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Card>
          <p className="text-text-2 text-xs">Total odds</p>
          <p className="num text-xl font-semibold">{formatOdds(ticket.total_odds)}</p>
        </Card>
        <Card>
          <p className="text-text-2 text-xs">Bookmaker-implied</p>
          <p className="num text-xl font-semibold">
            {percent(ticket.bookmaker_implied_probability, 2)}
          </p>
          <p className="text-muted text-xs">{oneInN(ticket.bookmaker_implied_probability)}</p>
        </Card>
        <Card>
          <p className="text-text-2 text-xs">Next kickoff</p>
          <p className="text-xl font-semibold">
            <Countdown to={ticket.next_kickoff} />
          </p>
        </Card>
        <Card>
          <p className="text-text-2 text-xs">Started legs</p>
          <p className="num text-xl font-semibold">
            {ticket.started_legs} of {ticket.legs.length}
          </p>
        </Card>
      </div>
      <p className="text-muted mt-2 text-xs">{ticket.implied_note}</p>

      {(ticket.superseded_by || ticket.rebooked_by || ticket.rebook_of) && (
        <Card className="mt-4 text-sm">
          {ticket.rebooked_by && (
            <p>
              Rebooked as{" "}
              <Link href={`/tickets/${ticket.rebooked_by}`} className="text-amber underline">
                a newer ticket
              </Link>
              .
            </p>
          )}
          {ticket.superseded_by && !ticket.rebooked_by && (
            <p>
              Superseded by{" "}
              <Link href={`/runs/${ticket.superseded_by}`} className="text-amber underline">
                a later run
              </Link>
              .
            </p>
          )}
          {ticket.rebook_of && (
            <p>
              A rebook of{" "}
              <Link href={`/tickets/${ticket.rebook_of}`} className="underline">
                an earlier ticket
              </Link>
              , without its started legs.
            </p>
          )}
        </Card>
      )}

      <Section title="Legs">
        <ul className="space-y-2">
          {ticket.legs.map((leg) => (
            <li
              key={`${leg.event_id}-${leg.kind}`}
              className={`bg-surface border-line rounded-md border p-3 text-sm ${
                leg.started ? "opacity-60" : ""
              }`}
            >
              <div className="flex justify-between gap-2">
                <span className="font-semibold">{legPick(leg)}</span>
                <span className="num">{formatOdds(leg.odds)}</span>
              </div>
              <p className="text-text-2 mt-0.5">
                {leg.away} at {leg.home} · {lagosDateTime(leg.kickoff)} Lagos
                {leg.started ? " · started" : ""}
              </p>
              <dl className="num text-text-2 mt-2 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
                <dt>Projection</dt>
                <dd className="text-right">{leg.projection.toFixed(1)}</dd>
                <dt>Main line</dt>
                <dd className="text-right">
                  {leg.main_line} @ {formatOdds(leg.main_odds)}
                </dd>
                <dt>Booked line ({MODE_LABELS[leg.mode] ?? leg.mode})</dt>
                <dd className="text-right">{leg.booked_line}</dd>
                <dt>Edge vs main</dt>
                <dd className="text-right">{points(leg.edge_main)}</dd>
                <dt>Edge vs booked</dt>
                <dd className="text-right">{points(leg.edge_booked)}</dd>
              </dl>
              {leg.flags.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {leg.flags.map((flag) => (
                    <FlagBadge key={flag} label={FLAG_LABELS[flag] ?? flag} />
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      </Section>
    </>
  );
}
