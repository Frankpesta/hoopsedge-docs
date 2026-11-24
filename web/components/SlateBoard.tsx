"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import useSWR from "swr";

import { runNow } from "@/lib/actions";
import { lagosDateTime } from "@/lib/format";
import type { Slate, SlateSport, Sport } from "@/lib/types";

import { SlateTable } from "./SlateTable";
import { TicketCard } from "./TicketCard";
import { Empty, StatusChip } from "./ui";

const POLL_MS = 5000;
const GIVE_UP_MS = 10 * 60 * 1000; // A run that never reports back shows on the Runs page instead.

async function fetchSlate(url: string): Promise<Slate> {
  const response = await fetch(url);
  if (!response.ok) throw new Error("Couldn't refresh");
  return (await response.json()) as Slate;
}

function RunNow({
  sport,
  date,
  disabled,
  realCodes,
  onStarted,
}: {
  sport: Sport;
  date: string;
  disabled: boolean;
  realCodes: boolean;
  onStarted: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function open() {
    key.current = crypto.randomUUID(); // One key per sheet, so a double tap can't start two runs.
    setMessage(null);
    dialog.current?.showModal();
  }

  function confirm() {
    start(async () => {
      const result = await runNow(sport, date, key.current);
      if (result.ok) {
        dialog.current?.close();
        onStarted();
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
        {disabled ? "Running" : `Run ${sport} now`}
      </button>
      <dialog
        ref={dialog}
        aria-labelledby={`run-${sport}`}
        className="bg-surface text-text border-line m-auto w-[min(92vw,26rem)] rounded-lg border p-5 backdrop:bg-black/60"
      >
        <h2 id={`run-${sport}`} className="font-display text-2xl font-extrabold uppercase">
          Run {sport} now?
        </h2>
        <p className="text-text-2 mt-2 text-sm">
          {realCodes
            ? "This books real codes on SportyBet. It never places a bet. Earlier real tickets of the same kind become superseded. "
            : "This builds new tickets with practice codes. "}
          It covers the {date} slate, with fresh ESPN results and SportyBet lines.
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

function emptyReason(view: SlateSport): string {
  if (!view.run) return "This slate hasn't been run yet.";
  if (view.run.status === "failed") return `The last run failed: ${view.run.error ?? "see Runs"}.`;
  return "No game cleared its threshold, so nothing was booked. Weak legs are dropped, never forced.";
}

function SportBoard({
  view,
  date,
  starting,
  realCodes,
  onStarted,
}: {
  view: SlateSport;
  date: string;
  starting: boolean;
  realCodes: boolean;
  onStarted: () => void;
}) {
  const running = starting || view.run?.status === "running";
  const current = view.tickets.filter((ticket) => ticket.current);
  const earlier = view.tickets.filter((ticket) => !ticket.current);
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {view.run ? (
            <>
              <StatusChip status={view.run.status} />
              <span className="text-text-2">
                {lagosDateTime(view.run.started_at)} · {view.run.trigger}
                {view.run.dry_run ? " · practice" : ""}
              </span>
            </>
          ) : (
            <StatusChip status="none" label="Not run yet" />
          )}
        </div>
        <RunNow
          sport={view.sport}
          date={date}
          disabled={running}
          realCodes={realCodes}
          onStarted={onStarted}
        />
      </div>
      {view.run?.error && <p className="text-loss mt-2 text-sm">{view.run.error}</p>}

      <h2 className="font-display mt-5 mb-2 text-xl font-semibold uppercase">Tickets</h2>
      <div className="space-y-4">
        {current.map((ticket) => (
          <TicketCard key={ticket.ticket_id} ticket={ticket} realCodes={realCodes} />
        ))}
        {!current.length && <Empty title="No tickets on this slate">{emptyReason(view)}</Empty>}
      </div>
      {earlier.length > 0 && (
        <details className="mt-4">
          <summary className="text-text-2 min-h-11 cursor-pointer py-2 text-sm">
            Earlier tickets ({earlier.length})
          </summary>
          <div className="mt-2 space-y-4">
            {earlier.map((ticket) => (
              <TicketCard key={ticket.ticket_id} ticket={ticket} realCodes={realCodes} />
            ))}
          </div>
        </details>
      )}

      <div className="mt-6 mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-xl font-semibold uppercase">
          Slate <span className="text-muted num text-base">{view.rows.length} games</span>
        </h2>
        <div className="flex gap-3 text-sm">
          {view.unmatched > 0 && (
            <Link href="/unmatched" className="text-amber font-semibold underline">
              {view.unmatched} unmatched
            </Link>
          )}
          {view.csv && (
            <a href={`/api/csv/${date}/${view.sport}`} className="text-text-2 underline">
              Download CSV
            </a>
          )}
        </div>
      </div>
      {view.rows.length ? (
        <SlateTable rows={view.rows} />
      ) : (
        <Empty title="No games yet">
          Run the slate to see its games and the model&apos;s leans.
        </Empty>
      )}
    </div>
  );
}

export function SlateBoard({
  initial,
  sport,
  realCodes,
}: {
  initial: Slate;
  sport: Sport;
  realCodes: boolean;
}) {
  const router = useRouter();
  const [, startRefresh] = useTransition();
  const [chosen, setChosen] = useState<Sport>(sport);
  const serverRunning = initial.sports.some((item) => item.run?.status === "running");
  // Run now's process takes a few seconds to record its run, so the board waits for it to end.
  const [awaited, setAwaited] = useState<{ sport: Sport; since: number } | null>(null);
  const [spell, setSpell] = useState(serverRunning ? 1 : 0);
  const [polling, setPolling] = useState(serverRunning);
  const { data } = useSWR<Slate>(
    polling ? `/api/slate/${initial.slate_date}?poll=${spell}` : null,
    fetchSlate,
    { refreshInterval: POLL_MS, revalidateOnFocus: false },
  );
  const slate = polling && data ? data : initial;

  useEffect(() => {
    if (!polling || !data) return;
    const running = data.sports.some((item) => item.run?.status === "running");
    if (awaited) {
      const run = data.sports.find((item) => item.sport === awaited.sport)?.run;
      const ended =
        run?.started_at &&
        new Date(run.started_at).getTime() >= awaited.since - 60_000 &&
        run.status !== "running";
      if (!ended && Date.now() - awaited.since < GIVE_UP_MS) return;
    } else if (running) {
      return;
    }
    setAwaited(null);
    setPolling(false);
    startRefresh(() => router.refresh());
  }, [polling, data, awaited, router]);

  function choose(next: Sport) {
    setChosen(next);
    const url = new URL(window.location.href);
    url.searchParams.set("sport", next);
    window.history.replaceState(null, "", url);
  }

  const view = slate.sports.find((item) => item.sport === chosen) ?? slate.sports[0];
  if (!view) return null;
  return (
    <>
      <div className="flex items-center gap-2">
        <div role="tablist" aria-label="Sport" className="flex items-center gap-1">
          {slate.sports.map((item) => (
            <button
              key={item.sport}
              type="button"
              role="tab"
              aria-selected={item.sport === view.sport}
              onClick={() => choose(item.sport)}
              className={`font-display min-h-11 rounded-md px-4 text-xl font-semibold tracking-wide uppercase ${
                item.sport === view.sport ? "bg-surface-2 text-text" : "text-muted"
              }`}
            >
              {item.sport}
              <span className="num ml-1 text-xs font-normal">
                {item.tickets.filter((ticket) => ticket.current).length || ""}
              </span>
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
        <SportBoard
          key={view.sport}
          view={view}
          date={slate.slate_date}
          starting={awaited?.sport === view.sport}
          realCodes={realCodes}
          onStarted={() => {
            setAwaited({ sport: view.sport, since: Date.now() });
            setSpell((value) => value + 1);
            setPolling(true);
          }}
        />
      </div>
    </>
  );
}
