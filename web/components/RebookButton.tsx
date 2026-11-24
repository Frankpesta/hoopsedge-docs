"use client";

import { useRef, useState, useTransition } from "react";

import { rebookTicket } from "@/lib/actions";

/** Brief §7: drop the started legs, re-check the rest, book a new code, link the two tickets. */
export function RebookButton({
  ticketId,
  startedLegs,
  realCodes,
}: {
  ticketId: string;
  startedLegs: number;
  realCodes: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  function open() {
    key.current = crypto.randomUUID(); // One key per sheet, so a double tap can't rebook twice.
    setMessage(null);
    dialog.current?.showModal();
  }

  function confirm() {
    start(async () => {
      const result = await rebookTicket(ticketId, key.current);
      if (result.ok) {
        setMessage("Rebooking: the new ticket appears here within a minute.");
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
        className="border-amber text-amber min-h-11 rounded-md border px-4 text-sm font-semibold"
      >
        Rebook
      </button>
      <dialog
        ref={dialog}
        aria-labelledby={`rebook-${ticketId}`}
        className="bg-surface text-text border-line m-auto w-[min(92vw,26rem)] rounded-lg border p-5 backdrop:bg-black/60"
      >
        <h2 id={`rebook-${ticketId}`} className="font-display text-2xl font-extrabold uppercase">
          Rebook this ticket?
        </h2>
        <p className="text-text-2 mt-2 text-sm">
          {startedLegs
            ? `${startedLegs} leg${startedLegs === 1 ? " has" : "s have"} started and will be dropped. `
            : "No leg has started yet. "}
          The rest are checked against fresh lines, and a new{" "}
          {realCodes ? "real SportyBet code" : "practice code"} is made. This ticket is kept and
          linked to the new one.
        </p>
        {message && (
          <p role="status" className="mt-3 text-sm">
            {message}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => dialog.current?.close()}
            className="border-line min-h-11 rounded-md border px-4 text-sm"
          >
            Close
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={confirm}
            className="bg-amber text-on-amber min-h-11 rounded-md px-4 text-sm font-semibold disabled:opacity-50"
          >
            {pending ? "Starting" : "Rebook"}
          </button>
        </div>
      </dialog>
    </>
  );
}
