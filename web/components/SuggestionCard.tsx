"use client";

import { useRef, useState, useTransition } from "react";

import { applySuggestion } from "@/lib/actions";
import { MARKET_NAMES, MODE_LABELS, percent, signedPercent, thresholdText } from "@/lib/format";
import type { Suggestion } from "@/lib/types";

type Numbers = Suggestion["current"]["numbers"];

function Figures({ label, numbers }: { label: string; numbers: Numbers }) {
  return (
    <div>
      <p className="text-text-2 text-xs">{label}</p>
      <dl className="num mt-1 grid grid-cols-4 gap-1 text-center text-sm">
        <div>
          <dt className="text-muted text-[11px]">Legs</dt>
          <dd>{numbers.legs}</dd>
        </div>
        <div>
          <dt className="text-muted text-[11px]">Hit</dt>
          <dd>{percent(numbers.hit_rate)}</dd>
        </div>
        <div>
          <dt className="text-muted text-[11px]">Implied</dt>
          <dd>{percent(numbers.implied)}</dd>
        </div>
        <div>
          <dt className="text-muted text-[11px]">ROI</dt>
          <dd>{signedPercent(numbers.roi)}</dd>
        </div>
      </dl>
    </div>
  );
}

/** Brief §9: a suggestion with its numbers. Nothing changes until the owner applies it. */
export function SuggestionCard({
  sport,
  suggestion,
  activeMode,
  version,
  minGraded,
}: {
  sport: string;
  suggestion: Suggestion;
  activeMode: string;
  version: number;
  minGraded: number;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef("");
  const [message, setMessage] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);
  const [pending, start] = useTransition();
  const best = suggestion.suggested;
  const mode = MODE_LABELS[suggestion.mode] ?? suggestion.mode;
  const title = `${MARKET_NAMES[suggestion.market] ?? suggestion.market}, ${mode.toLowerCase()}`;
  const same =
    best !== null &&
    best !== undefined &&
    suggestion.mode === activeMode &&
    thresholdText(best.thresholds) === thresholdText(suggestion.current.thresholds);

  function open() {
    key.current = crypto.randomUUID(); // One key per sheet, so a double tap applies once.
    setMessage(null);
    dialog.current?.showModal();
  }

  function confirm() {
    if (!best) return;
    start(async () => {
      const result = await applySuggestion(
        sport,
        { market: suggestion.market, mode: suggestion.mode, thresholds: best.thresholds },
        version,
        key.current,
      );
      if (result.ok) {
        setApplied(true);
        setMessage(`Applied as config version ${result.data.version}. The next run uses it.`);
      } else {
        setMessage(result.message);
      }
    });
  }

  return (
    <article className="bg-surface border-line rounded-lg border p-4">
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-display text-xl font-extrabold uppercase">{title}</h3>
          <p className="text-text-2 text-xs">
            {suggestion.graded} graded legs over {suggestion.slates} slates
            {suggestion.mode === activeMode ? " · the line in use" : ""}
          </p>
        </div>
      </header>
      <div className="mt-3 space-y-3">
        <Figures
          label={`Now: ${thresholdText(suggestion.current.thresholds)}`}
          numbers={suggestion.current.numbers}
        />
        {best ? (
          <Figures label={`Suggested: ${thresholdText(best.thresholds)}`} numbers={best.numbers} />
        ) : (
          <p className="text-text-2 text-sm">
            Not enough yet. A suggestion needs {minGraded} graded legs clearing it (
            {suggestion.graded} graded so far).
          </p>
        )}
      </div>
      {best && (
        <footer className="mt-3 flex items-center justify-end gap-2">
          {same ? (
            <p className="text-text-2 text-sm">Already in use.</p>
          ) : (
            <button
              type="button"
              onClick={open}
              disabled={applied}
              className="border-amber text-amber min-h-11 rounded-md border px-4 text-sm font-semibold disabled:opacity-50"
            >
              {applied ? "Applied" : "Apply"}
            </button>
          )}
        </footer>
      )}
      {applied && message && <p className="mt-2 text-sm">{message}</p>}
      <dialog
        ref={dialog}
        aria-labelledby={`apply-${sport}-${suggestion.market}-${suggestion.mode}`}
        className="bg-surface text-text border-line m-auto w-[min(92vw,26rem)] rounded-lg border p-5 backdrop:bg-black/60"
      >
        <h2
          id={`apply-${sport}-${suggestion.market}-${suggestion.mode}`}
          className="font-display text-2xl font-extrabold uppercase"
        >
          Apply this suggestion?
        </h2>
        <p className="text-text-2 mt-2 text-sm">
          {sport} {title.toLowerCase()}: thresholds {best ? thresholdText(best.thresholds) : ""},
          booked at the {mode.toLowerCase()}. This saves a new config version, which the next run
          uses. It was fitted on the same games it is scored on, so expect it to do a little worse
          than these numbers.
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
            disabled={pending || applied}
            onClick={confirm}
            className="bg-amber text-on-amber min-h-11 rounded-md px-4 text-sm font-semibold disabled:opacity-50"
          >
            {pending ? "Saving" : "Apply"}
          </button>
        </div>
      </dialog>
    </article>
  );
}
