"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import useSWR from "swr";

import { runNow } from "@/lib/actions";
import { FAMILIES, FAMILY_NAMES, lagosTime, reasonLabel } from "@/lib/format";
import type { Slate, SlateLeague } from "@/lib/types";

import { TicketCardView } from "./TicketCardView";
import { Empty, StatusChip } from "./ui";

const POLL_MS = 5000;
const GIVE_UP_MS = 5 * 60 * 1000; // A run that never reports back shows on the Runs page instead.

async function fetchSlate(url: string): Promise<Slate> {
  const response = await fetch(url);
  if (!response.ok) throw new Error("Couldn't refresh");
  return (await response.json()) as Slate;
}

function isRunning(slate: Slate): boolean {
  return slate.leagues.some((league) => league.run?.status === "running");
}

function RunNow({
  league,
  date,
  disabled,
  realCodes,
  onStarted,
}: {
  league: string;
  date: string;
  disabled: boolean;
  realCodes: boolean;
  onStarted: (runId: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function open() {
    // One key per confirmation sheet, so a double tap can't start two runs.
    key.current = crypto.randomUUID();
    setMessage(null);
    dialog.current?.showModal();
  }

  function confirm() {
    start(async () => {
      const result = await runNow(league, date, key.current);
      if (result.ok) {
        dialog.current?.close();
        onStarted(result.data.run_id);
      } else {
        setMessage(result.message);
      }
    });
  }

  return (
    <>
      <button
        type="button"
        onClick={open}
        disabled={disabled}
        className="bg-amber text-on-amber min-h-11 rounded-md px-4 text-sm font-semibold disabled:opacity-50"
      >
        {disabled ? "Running" : `Run ${league} now`}
      </button>
      <dialog
        ref={dialog}
        aria-labelledby={`run-${league}`}
        className="bg-surface text-text border-line m-auto w-[min(92vw,26rem)] rounded-lg border p-5 backdrop:bg-black/60"
      >
        <h2 id={`run-${league}`} className="font-display text-2xl font-extrabold uppercase">
          Run {league} now?
        </h2>
        <p className="text-text-2 mt-2 text-sm">
          {realCodes
            ? "This books real codes on SportyBet. It never places a bet. "
            : "This builds new tickets with practice codes. "}
          It covers {date}. Any current {league} tickets for that date become superseded, so their
          codes stop showing here.
        </p>
        {message && (
          <p role="alert" className="text-loss mt-2 text-sm">
            {message}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => dialog.current?.close()}
            className="border-line min-h-11 rounded-md border px-4 text-sm"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={confirm}
            disabled={pending}
            className="bg-amber text-on-amber min-h-11 rounded-md px-4 text-sm font-semibold disabled:opacity-50"
          >
            {pending ? "Starting" : "Start the run"}
          </button>
        </div>
      </dialog>
    </>
  );
}

function emptyReason(league: SlateLeague): string {
  if (!league.run) {
    return league.games
      ? `${league.games} games are scheduled, but this slate hasn't been run yet.`
      : "No games are scheduled for this date.";
  }
  if (league.run.status === "failed")
    return `The last run failed: ${league.run.error ?? "see Runs"}.`;
  return "No market had enough strong legs, so nothing was booked. That's the system working: weak legs are dropped, not forced.";
}

function LeagueBoard({
  league,
  date,
  starting,
  realCodes,
  onStarted,
}: {
  league: SlateLeague;
  date: string;
  starting: boolean;
  realCodes: boolean;
  onStarted: (runId: string) => void;
}) {
  const [family, setFamily] = useState<string | null>(null);
  const firstWithTickets = FAMILIES.find((name) => (league.markets[name]?.length ?? 0) > 0);
  const chosen = family ?? firstWithTickets ?? FAMILIES[0];
  const tickets = league.markets[chosen] ?? [];
  const running = starting || league.run?.status === "running";
  const excluded = Object.entries(league.excluded).sort((a, b) => b[1] - a[1]);
  const anyTickets = FAMILIES.some((name) => (league.markets[name]?.length ?? 0) > 0);

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {league.run ? (
            <StatusChip status={league.run.status} />
          ) : (
            <StatusChip status="none" label="Not run yet" />
          )}
          {league.first_tip && (
            <span className="text-text-2">First tip {lagosTime(league.first_tip)} Lagos</span>
          )}
        </div>
        <RunNow
          league={league.league}
          date={date}
          disabled={running}
          realCodes={realCodes}
          onStarted={onStarted}
        />
      </div>

      <div
        role="tablist"
        aria-label="Market"
        className="bg-surface-2 mt-4 grid grid-cols-4 gap-1 rounded-lg p-1"
      >
        {FAMILIES.map((name) => {
          const count = league.markets[name]?.length ?? 0;
          return (
            <button
              key={name}
              type="button"
              role="tab"
              aria-selected={chosen === name}
              onClick={() => setFamily(name)}
              className={`min-h-11 rounded-md px-1 text-xs font-semibold sm:text-sm ${
                chosen === name ? "bg-surface text-amber" : "text-text-2"
              }`}
            >
              {FAMILY_NAMES[name]}
              <span className="text-muted num ml-1 font-normal">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="mt-4 space-y-4">
        {tickets.map((ticket, index) => (
          <TicketCardView key={ticket.ticket_id} ticket={ticket} index={index} />
        ))}
        {!tickets.length && (
          <Empty
            title={
              anyTickets
                ? `No ${FAMILY_NAMES[chosen]} tickets on this slate`
                : "No tickets on this slate"
            }
          >
            {anyTickets
              ? "This market didn't have enough strong legs. Other markets did."
              : emptyReason(league)}
          </Empty>
        )}
      </div>

      {(excluded.length > 0 || league.unmatched_events > 0) && (
        <details className="bg-surface border-line mt-4 rounded-lg border p-4">
          <summary className="min-h-6 cursor-pointer font-semibold">
            Excluded ({excluded.reduce((total, [, count]) => total + count, 0)})
          </summary>
          <ul className="mt-3 space-y-1 text-sm">
            {excluded.map(([reason, count]) => (
              <li key={reason} className="flex justify-between gap-4">
                <span className="text-text-2">{reasonLabel(reason)}</span>
                <span className="num">{count}</span>
              </li>
            ))}
          </ul>
          {league.unmatched_events > 0 && (
            <Link
              href="/unmatched"
              className="text-amber mt-3 block text-sm font-semibold underline"
            >
              {league.unmatched_events} events need a match
            </Link>
          )}
        </details>
      )}
    </div>
  );
}

export function SlateBoard({ initial, realCodes }: { initial: Slate; realCodes: boolean }) {
  const router = useRouter();
  const [, startRefresh] = useTransition();
  const serverRunning = isRunning(initial);
  // A run takes seconds to record itself, so Run now follows its own ID until that run has ended.
  const [awaited, setAwaited] = useState<{ league: string; runId: string; since: number } | null>(
    null,
  );
  // Each spell of polling has its own SWR key, so an old answer can't end a new spell at once.
  const [spell, setSpell] = useState(serverRunning ? 1 : 0);
  const [polling, setPolling] = useState(serverRunning);
  useEffect(() => {
    if (serverRunning) setPolling(true);
  }, [serverRunning]);
  const { data } = useSWR<Slate>(
    polling ? `/api/slate/${initial.date}?poll=${spell}` : null,
    fetchSlate,
    { refreshInterval: POLL_MS, revalidateOnFocus: false },
  );
  const slate = polling && data ? data : initial;
  const leagues = slate.leagues;

  function started(league: string, runId: string) {
    setAwaited({ league, runId, since: Date.now() });
    setSpell((value) => value + 1);
    setPolling(true);
  }

  useEffect(() => {
    if (!polling || !data) return;
    if (awaited) {
      const run = data.leagues.find((item) => item.league === awaited.league)?.run;
      const ended = run?.run_id === awaited.runId && run.status !== "running";
      if (!ended && Date.now() - awaited.since < GIVE_UP_MS) return;
    } else if (isRunning(data)) {
      return;
    }
    // Finished, which changes more than this board shows, so reload the whole page.
    setAwaited(null);
    setPolling(false);
    startRefresh(() => router.refresh());
  }, [polling, data, awaited, router]);

  const [chosen, setChosen] = useState<string | null>(null);
  const withTickets = leagues.find((league) =>
    Object.values(league.markets).some((tickets) => tickets.length > 0),
  );
  const current =
    leagues.find((item) => item.league === (chosen ?? withTickets?.league)) ?? leagues[0];

  if (!current) return null;
  return (
    <>
      <div className="flex items-center gap-2">
        <div role="tablist" aria-label="League" className="flex items-center gap-2">
          {leagues.map((league) => (
            <button
              key={league.league}
              type="button"
              role="tab"
              aria-selected={league.league === current.league}
              onClick={() => setChosen(league.league)}
              className={`font-display min-h-11 rounded-md px-5 text-xl font-semibold tracking-wide uppercase ${
                league.league === current.league ? "bg-surface-2 text-text" : "text-muted"
              }`}
            >
              {league.league}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => startRefresh(() => router.refresh())}
          className="text-text-2 ml-auto min-h-11 px-2 text-sm underline"
        >
          Refresh
        </button>
      </div>
      <div className="mt-3">
        <LeagueBoard
          key={current.league}
          league={current}
          date={slate.date}
          starting={awaited?.league === current.league}
          realCodes={realCodes}
          onStarted={(runId) => started(current.league, runId)}
        />
      </div>
    </>
  );
}
