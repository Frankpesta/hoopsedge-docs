"use client";

import { AnimatePresence, m } from "framer-motion";
import Link from "next/link";
import { useState } from "react";

import { FAMILY_NAMES, formatOdds, lagosTime, oneInN, percent, signedPercent } from "@/lib/format";
import type { LegSummary, TicketCard } from "@/lib/types";

import { TicketStub } from "./TicketStub";
import { ShotClock, StatusChip } from "./ui";

function LegRow({ leg }: { leg: LegSummary }) {
  const hidden = leg.p_calibrated === null;
  return (
    <li className="border-line border-t py-3 first:border-t-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold">{leg.pick}</p>
          <Link
            href={`/games/${leg.game_id}`}
            className="text-text-2 block text-sm underline-offset-2 hover:underline"
          >
            {leg.matchup}
          </Link>
        </div>
        <div className="shrink-0 text-right">
          <p className="num font-semibold">{formatOdds(leg.odds)}</p>
          <p className="text-muted text-xs">{lagosTime(leg.tip_time)}</p>
        </div>
      </div>
      {!hidden && leg.p_calibrated !== null && (
        <div className="mt-2">
          <ShotClock value={leg.p_calibrated} fair={leg.fair} />
          <p className="text-text-2 num mt-1 text-xs">
            Model {percent(leg.p_calibrated)} · Implied {percent(leg.fair)} · Edge{" "}
            {signedPercent(leg.edge)}
          </p>
        </div>
      )}
      {hidden && <p className="text-muted num mt-1 text-xs">Implied {percent(leg.fair)}</p>}
    </li>
  );
}

/** D78: these legs are only here because the owner chose to fill every market. */
export function WeakLegs({ weak, legs }: { weak: number; legs: number }) {
  if (!weak) return null;
  return (
    <p className="text-text-2 mt-2 text-sm">
      <span aria-hidden>{"! "}</span>
      {weak} of {legs} legs are below the minimum testing chose for this market.
    </p>
  );
}

export function TicketCardView({ ticket, index = 0 }: { ticket: TicketCard; index?: number }) {
  const [open, setOpen] = useState(false);
  const odds = oneInN(ticket.combined_prob);
  const unproven = ticket.edge_label === "unproven";
  return (
    <m.article
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className="bg-surface border-line rounded-lg border p-4"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-lg font-semibold tracking-wide uppercase">
          {FAMILY_NAMES[ticket.family] ?? ticket.family} · Ticket {ticket.ticket_no}
        </h2>
        <div className="flex items-center gap-2">
          <StatusChip status={ticket.status} />
          <span className="text-muted text-xs">v{ticket.version}</span>
        </div>
      </div>

      <TicketStub code={ticket.booking_code ?? null} status={ticket.status} />
      {ticket.reason && <p className="text-loss mt-2 text-sm">{ticket.reason}</p>}

      <dl className="mt-3 grid grid-cols-3 gap-2">
        <div>
          <dt className="text-muted text-xs">Legs</dt>
          <dd className="num font-semibold">{ticket.legs}</dd>
        </div>
        <div>
          <dt className="text-muted text-xs">Odds</dt>
          <dd className="num font-semibold">{formatOdds(ticket.combined_odds)}</dd>
        </div>
        <div>
          <dt className="text-muted text-xs">Chance</dt>
          <dd className="num font-semibold">{unproven ? "Unproven" : (odds ?? "n/a")}</dd>
        </div>
        {!unproven && (
          <div>
            <dt className="text-muted text-xs">EV</dt>
            <dd className="num font-semibold">{signedPercent(ticket.expected_value, 0)}</dd>
          </div>
        )}
      </dl>

      {ticket.probability_note && (
        <p className="border-line text-text-2 mt-3 border-l-2 pl-3 text-sm">
          {ticket.probability_note}
        </p>
      )}
      {!ticket.passed_gate && (
        <p className="text-loss mt-2 text-sm">
          <span aria-hidden>{"✕ "}</span>
          This market failed its evidence gate. It&apos;s on because you chose to keep it on.
        </p>
      )}

      <WeakLegs weak={ticket.weak_legs ?? 0} legs={ticket.legs} />

      <div className="mt-3 flex items-center justify-between gap-3">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="text-amber min-h-11 text-sm font-semibold"
        >
          {open ? "Hide legs" : `Show ${ticket.legs} legs`}
        </button>
        <Link href={`/tickets/${ticket.ticket_id}`} className="text-text-2 text-sm underline">
          Full ticket
        </Link>
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <m.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            {ticket.legs_summary?.map((leg) => (
              <LegRow key={leg.rank} leg={leg} />
            ))}
          </m.ul>
        )}
      </AnimatePresence>
    </m.article>
  );
}
