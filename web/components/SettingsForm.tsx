"use client";

import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, useTransition } from "react";

import { saveSettings } from "@/lib/actions";
import { diffSettings } from "@/lib/format";
import type { SettingsDocument } from "@/lib/types";

const SCHEDULES: readonly (readonly [string, string])[] = [
  ["ncaab_slate", "NCAAB slate"],
  ["ncaab_late_slate", "NCAAB late slate"],
  ["nba_slate", "NBA slate"],
  ["nba_late_slate", "NBA late slate"],
  ["ncaaf_slate", "NCAAF slate"],
  ["ncaaf_saturday_slate", "NCAAF Saturday slate"],
  ["ncaaf_scores", "NCAAF scores"],
  ["ncaab_scores", "NCAAB scores"],
  ["nba_scores", "NBA scores"],
  ["backup", "Database backup"],
];
const INPUT = "bg-surface-2 border-line text-text min-h-11 w-full rounded-md border px-2 text-sm";

function Toggle({
  label,
  checked,
  onChange,
  note,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  note?: string;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3">
      <span>
        <span className="text-sm">{label}</span>
        {note && <span className="text-muted block text-xs">{note}</span>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="accent-amber h-5 w-5 shrink-0"
      />
    </label>
  );
}

export function SettingsForm({
  version,
  document: saved,
}: {
  version: number;
  document: SettingsDocument;
}) {
  const [draft, setDraft] = useState<SettingsDocument>(() => structuredClone(saved));
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, start] = useTransition();
  const key = useRef<string | null>(null);
  const router = useRouter();
  const changes = useMemo(() => diffSettings(saved, draft), [saved, draft]);

  function update(change: (next: SettingsDocument) => void) {
    setMessage(null);
    setDraft((current) => {
      const next = structuredClone(current);
      change(next);
      return next;
    });
  }

  function save() {
    key.current ??= crypto.randomUUID();
    start(async () => {
      const result = await saveSettings(version, draft, key.current as string);
      key.current = null;
      if (result.ok) {
        setMessage({ ok: true, text: `Saved as version ${result.data.version}.` });
        router.refresh();
      } else {
        setMessage({ ok: false, text: result.message });
      }
    });
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <fieldset className="bg-surface border-line rounded-lg border p-4">
        <legend className="font-display px-1 text-lg font-semibold uppercase">Safety</legend>
        <Toggle
          label="Dry run"
          note="On: tickets get practice codes, and nothing is booked."
          checked={draft.dry_run}
          onChange={(value) => update((next) => void (next.dry_run = value))}
        />
        <Toggle
          label="Pause all scheduled runs"
          note="Run now still works."
          checked={draft.pause_all}
          onChange={(value) => update((next) => void (next.pause_all = value))}
        />
        <Toggle
          label="Route SportyBet through the proxy"
          note="Only needed if SportyBet blocks AWS."
          checked={draft.sportybet_proxy_enabled}
          onChange={(value) => update((next) => void (next.sportybet_proxy_enabled = value))}
        />
      </fieldset>

      <fieldset className="bg-surface border-line rounded-lg border p-4">
        <legend className="font-display px-1 text-lg font-semibold uppercase">
          Schedules (Lagos)
        </legend>
        <div className="grid grid-cols-3 gap-2">
          {SCHEDULES.map(([name, label]) => (
            <label key={name} className="text-text-2 text-xs">
              {label}
              <input
                type="time"
                value={draft.schedules[name] ?? ""}
                onChange={(event) =>
                  update((next) => void (next.schedules[name] = event.target.value))
                }
                className={`${INPUT} mt-1`}
              />
            </label>
          ))}
        </div>
      </fieldset>

      <p className="text-muted text-xs">
        Thresholds, line modes and ticket sizes are in the v2 config document, which gets its own
        page in the dashboard rebuild (B5).
      </p>

      <div className="bg-surface border-line sticky bottom-16 rounded-lg border p-4">
        <p className="font-semibold">
          {changes.length ? `${changes.length} unsaved changes` : "No unsaved changes"}
        </p>
        {changes.length > 0 && (
          <ul className="num text-text-2 mt-1 max-h-32 space-y-0.5 overflow-y-auto text-xs">
            {changes.map((change) => (
              <li key={change.path} className="break-all">
                {change.path}: {String(change.from)} {"→"} {String(change.to)}
              </li>
            ))}
          </ul>
        )}
        {message && (
          <p role="status" className={`mt-2 text-sm ${message.ok ? "" : "text-loss"}`}>
            {message.text}
          </p>
        )}
        <button
          type="submit"
          disabled={pending || !changes.length}
          className="bg-amber text-on-amber mt-2 min-h-11 w-full rounded-md font-semibold disabled:opacity-50"
        >
          {pending ? "Saving" : "Save changes"}
        </button>
      </div>
    </form>
  );
}
