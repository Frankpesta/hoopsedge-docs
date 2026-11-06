"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, useTransition } from "react";

import { rollbackModel } from "@/lib/actions";

export function Rollback({ league, version }: { league: string; version: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef("");
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const router = useRouter();

  function confirm() {
    start(async () => {
      const result = await rollbackModel(league, version, reason || "Rolled back", key.current);
      if (result.ok) {
        dialog.current?.close();
        router.refresh();
      } else {
        setMessage(result.message);
      }
    });
  }

  return (
    <>
      <button
        type="button"
        onClick={() => {
          key.current = crypto.randomUUID();
          setMessage(null);
          dialog.current?.showModal();
        }}
        className="text-amber min-h-8 text-sm font-semibold underline"
      >
        Roll back to this
      </button>
      <dialog
        ref={dialog}
        className="bg-surface text-text border-line m-auto w-[min(92vw,26rem)] rounded-lg border p-5 backdrop:bg-black/60"
      >
        <h2 className="font-display text-2xl font-extrabold uppercase">Roll back {league}?</h2>
        <p className="text-text-2 mt-2 text-sm">
          Predictions will use <span className="num break-all">{version}</span> from the next run.
          This is recorded, and you can change it again.
        </p>
        <label className="text-text-2 mt-3 block text-xs">
          Why?
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="bg-surface-2 border-line text-text mt-1 min-h-11 w-full rounded-md border px-2 text-sm"
          />
        </label>
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
            {pending ? "Rolling back" : "Roll back"}
          </button>
        </div>
      </dialog>
    </>
  );
}
