import Link from "next/link";
import { notFound } from "next/navigation";

import { TicketStub } from "@/components/TicketStub";
import { Card, PageTitle, Section, StatTile, StatusChip } from "@/components/ui";
import { apiGetOptional } from "@/lib/api";
import {
  FAMILY_NAMES,
  formatOdds,
  lagosTime,
  oneInN,
  percent,
  signedNumber,
  signedPercent,
} from "@/lib/format";
import type { LegDetail, TicketDetail } from "@/lib/types";

const MARKS: Record<string, string> = { won: "✓", lost: "✕", push: "=", void: "–" };

function pick(leg: LegDetail): string {
  return leg.pick ?? `${leg.family} ${leg.side} ${leg.line}`;
}

function LegRow({ leg }: { leg: LegDetail }) {
  return (
    <li className="border-line border-t py-3 first:border-t-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold">
            <span aria-hidden className="mr-1.5">
              {MARKS[leg.status] ?? "•"}
            </span>
            {pick(leg)}
          </p>
          <Link href={`/games/${leg.game_id}`} className="text-text-2 text-sm underline">
            {leg.matchup ?? `Game ${leg.game_id}`}
          </Link>
        </div>
        <div className="shrink-0 text-right">
          <p className="num font-semibold">{formatOdds(leg.odds)}</p>
          <StatusChip status={leg.status} />
        </div>
      </div>
      <p className="text-muted num mt-1 text-xs">
        {lagosTime(leg.tip_time)} · Implied {percent(leg.fair)}
        {leg.p_calibrated !== null && leg.p_calibrated !== undefined
          ? ` · Model ${percent(leg.p_calibrated)} · Edge ${signedPercent(leg.edge)}`
          : ""}
        {leg.actual_value !== null && leg.actual_value !== undefined
          ? ` · Result ${leg.actual_value}`
          : ""}
        {leg.model_error !== null && leg.model_error !== undefined
          ? ` · Model off by ${signedNumber(leg.model_error, 1)}`
          : ""}
      </p>
      {leg.correction && (
        <p className="text-loss mt-1 text-xs">The score was corrected after this first settled.</p>
      )}
    </li>
  );
}

/** What changed from the version before: the reason a code you copied may have gone stale. */
async function versionDiff(detail: TicketDetail) {
  const index = detail.versions.findIndex((row) => row.ticket_id === detail.ticket.ticket_id);
  const previous = index > 0 ? detail.versions[index - 1] : undefined;
  if (!previous) return null;
  const before = await apiGetOptional<TicketDetail>(`/tickets/${previous.ticket_id}`);
  if (!before) return null;
  const old = new Set(before.leg_details.map(pick));
  const now = new Set(detail.leg_details.map(pick));
  return {
    version: previous.version,
    added: [...now].filter((item) => !old.has(item)),
    removed: [...old].filter((item) => !now.has(item)),
  };
}

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const detail = await apiGetOptional<TicketDetail>(`/tickets/${encodeURIComponent(id)}`);
  if (!detail) notFound();
  const ticket = detail.ticket;
  const diff = await versionDiff(detail);
  const unproven = ticket.edge_label === "unproven";
  return (
    <>
      <PageTitle
        title={`${FAMILY_NAMES[ticket.family] ?? ticket.family} ticket ${ticket.ticket_no}`}
        sub={`${ticket.league} · Slate ${detail.slate_date} · Version ${ticket.version}`}
      />
      <TicketStub code={ticket.booking_code ?? null} status={ticket.status} />
      {ticket.reason && <p className="text-loss mt-2 text-sm">{ticket.reason}</p>}

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatTile
          label="Status"
          value={<StatusChip status={ticket.status} />}
          note={`Settlement: ${ticket.settle_status}`}
        />
        <StatTile label="Combined odds" value={formatOdds(ticket.combined_odds)} />
        <StatTile
          label="Chance"
          value={unproven ? "Unproven" : (oneInN(ticket.combined_prob) ?? "n/a")}
        />
        <StatTile
          label="Expected value"
          value={unproven ? "Unproven" : signedPercent(ticket.expected_value, 0)}
        />
      </div>
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
      {ticket.correction && (
        <p className="text-loss mt-2 text-sm">A score behind this ticket was corrected.</p>
      )}

      <Section title="Legs">
        <Card>
          <ul>
            {detail.leg_details.map((leg) => (
              <LegRow key={leg.rank} leg={leg} />
            ))}
          </ul>
        </Card>
      </Section>

      {detail.verification && (
        <Section title="Verification">
          <Card>
            <p className="text-sm">
              Selections missing:{" "}
              {(detail.verification.missing as unknown[] | undefined)?.length ?? 0} · Unexpected:{" "}
              {(detail.verification.extra as unknown[] | undefined)?.length ?? 0} · Odds moved:{" "}
              {(detail.verification.odds_differences as unknown[] | undefined)?.length ?? 0}
            </p>
          </Card>
        </Section>
      )}

      <Section title="Version history">
        <Card>
          <ul className="space-y-2 text-sm">
            {detail.versions.map((row) => (
              <li key={row.ticket_id} className="flex items-center justify-between gap-3">
                <Link href={`/tickets/${row.ticket_id}`} className="underline">
                  Version {row.version}
                </Link>
                <span className="num text-text-2 break-all">{row.booking_code ?? "No code"}</span>
                <StatusChip status={row.status} />
              </li>
            ))}
          </ul>
          {diff && (
            <div className="border-line mt-3 border-t pt-3 text-sm">
              <p className="font-semibold">Changes since version {diff.version}</p>
              {!diff.added.length && !diff.removed.length && (
                <p className="text-text-2 mt-1">Same legs.</p>
              )}
              {diff.added.map((item) => (
                <p key={item} className="mt-1">
                  + {item}
                </p>
              ))}
              {diff.removed.map((item) => (
                <p key={item} className="text-text-2 mt-1">
                  {"−"} {item}
                </p>
              ))}
            </div>
          )}
        </Card>
      </Section>
    </>
  );
}
