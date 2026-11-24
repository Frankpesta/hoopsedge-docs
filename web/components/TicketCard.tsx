"use client";

import Link from "next/link";

import {
  KIND_NAMES,
  MODE_LABELS,
  formatOdds,
  lagosTime,
  legPick,
  oneInN,
  percent,
  points,
} from "@/lib/format";
import type { LegView, TicketView } from "@/lib/types";

import { Countdown } from "./Countdown";
import { RebookButton } from "./RebookButton";
import { TicketStub } from "./TicketStub";
import { StatusChip } from "./ui";

function LegLine({ leg }: { leg: LegView }) {
  return (
    <li className={`flex justify-between gap-3 py-1.5 text-sm ${leg.started ? "opacity-50" : ""}`}>
      <span>
        <span className="font-medium">{legPick(leg)}</span>
        <span className="text-text-2 block text-xs">
          {leg.away} at {leg.home} · {lagosTime(leg.kickoff)}
          {leg.started ? " · started" : ""}
        </span>
      </span>
      <span className="num text-right text-xs">
        {formatOdds(leg.odds)}
        <span className="text-text-2 block">
          {points(leg.direction === "Over" ? leg.edge_booked : -leg.edge_booked)} cushion
        </span>
      </span>
    </li>
  );
}

export function TicketCard({ ticket, realCodes }: { ticket: TicketView; realCodes: boolean }) {
  const modes = [...new Set(ticket.legs.map((leg) => MODE_LABELS[leg.mode] ?? leg.mode))];
  return (
    <article
      className={`bg-surface border-line rounded-lg border p-4 ${ticket.current ? "" : "opacity-70"}`}
    >
      <header className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h3 className="font-display text-xl font-extrabold uppercase">
            {KIND_NAMES[ticket.kind] ?? ticket.kind} #{ticket.number}
          </h3>
          <p className="text-text-2 text-xs">
            {ticket.legs.length} legs · {modes.join(", ")}
          </p>
        </div>
        <StatusChip status={ticket.status} />
      </header>
      <TicketStub code={ticket.code ?? null} status={ticket.status} />
      {ticket.reason && <p className="text-loss mt-2 text-sm">{ticket.reason}</p>}
      <dl className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div>
          <dt className="text-text-2 text-xs">Total odds</dt>
          <dd className="num text-lg font-semibold">{formatOdds(ticket.total_odds)}</dd>
        </div>
        <div>
          <dt className="text-text-2 text-xs">Bookmaker-implied</dt>
          <dd className="num text-lg font-semibold">
            {ticket.bookmaker_implied_probability >= 0.001
              ? percent(ticket.bookmaker_implied_probability)
              : oneInN(ticket.bookmaker_implied_probability)}
          </dd>
        </div>
        <div>
          <dt className="text-text-2 text-xs">First kickoff</dt>
          <dd className="text-base font-semibold whitespace-nowrap">
            <Countdown to={ticket.next_kickoff} />
          </dd>
        </div>
      </dl>
      <p className="text-muted mt-2 text-xs">{ticket.implied_note}</p>
      {ticket.model_disagrees > 0 && (
        <p className="text-amber mt-2 text-sm">
          The model disagrees with {ticket.model_disagrees} of these legs at the booked line.
        </p>
      )}
      {!ticket.current && (
        <p className="text-text-2 mt-2 text-sm">
          {ticket.rebooked_by ? "Rebooked: use the newer ticket." : "Superseded by a later run."}
        </p>
      )}
      <details className="mt-3">
        <summary className="text-text-2 min-h-11 cursor-pointer py-2 text-sm">Legs</summary>
        <ul className="divide-line divide-y">
          {ticket.legs.map((leg) => (
            <LegLine key={`${leg.event_id}-${leg.kind}`} leg={leg} />
          ))}
        </ul>
      </details>
      <footer className="mt-3 flex items-center justify-between gap-2">
        <Link href={`/tickets/${ticket.ticket_id}`} className="text-text-2 text-sm underline">
          Details
        </Link>
        {ticket.current && ticket.status !== "invalid" && (
          <RebookButton
            ticketId={ticket.ticket_id}
            startedLegs={ticket.started_legs}
            realCodes={realCodes}
          />
        )}
      </footer>
    </article>
  );
}
