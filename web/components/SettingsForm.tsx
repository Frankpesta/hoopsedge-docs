"use client";

import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, useTransition } from "react";

import { saveSettings } from "@/lib/actions";
import { diffSettings, FAMILIES, FAMILY_NAMES } from "@/lib/format";
import type { SettingsDocument } from "@/lib/types";

const SCHEDULES: readonly (readonly [keyof SettingsDocument["schedules"] & string, string])[] = [
  ["ncaa_slate", "NCAA slate"],
  ["nba_slate", "NBA slate"],
  ["nba_late_slate", "NBA late slate"],
  ["nightly_learn", "Nightly learning"],
];
const INPUT = "bg-surface-2 border-line text-text min-h-11 w-full rounded-md border px-2 text-sm";
const LEAGUES = ["NBA", "NCAA"] as const;

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

function parseNumber(text: string): number | null {
  if (!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
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

      <fieldset className="bg-surface border-line rounded-lg border p-4">
        <legend className="font-display px-1 text-lg font-semibold uppercase">Tickets</legend>
        <div className="grid grid-cols-2 gap-2">
          <label className="text-text-2 text-xs">
            Fewest legs on a ticket
            <input
              type="number"
              min={2}
              value={draft.min_legs}
              onChange={(event) =>
                update((next) => void (next.min_legs = Number(event.target.value)))
              }
              className={`${INPUT} mt-1`}
            />
          </label>
          <label className="text-text-2 text-xs">
            Minutes before tip to stop
            <input
              type="number"
              min={20}
              value={draft.min_minutes_before_tip}
              onChange={(event) =>
                update((next) => void (next.min_minutes_before_tip = Number(event.target.value)))
              }
              className={`${INPUT} mt-1`}
            />
          </label>
        </div>
        <Toggle
          label="Allow alternate lines"
          note="Off: only the most even line on each market."
          checked={draft.allow_alt_lines}
          onChange={(value) => update((next) => void (next.allow_alt_lines = value))}
        />
      </fieldset>

      {LEAGUES.map((league) => (
        <fieldset key={league} className="bg-surface border-line rounded-lg border p-4">
          <legend className="font-display px-1 text-lg font-semibold uppercase">{league}</legend>
          <Toggle
            label={`${league} on`}
            checked={draft.leagues[league]?.enabled ?? false}
            onChange={(value) =>
              update((next) => {
                const entry = next.leagues[league];
                if (entry) entry.enabled = value;
              })
            }
          />
          <div className="mt-2 space-y-3">
            {FAMILIES.map((family) => {
              const market = draft.markets[league]?.[family];
              if (!market) return null;
              const edit = (change: (item: NonNullable<typeof market>) => void) =>
                update((next) => {
                  const entry = next.markets[league]?.[family];
                  if (entry) change(entry);
                });
              return (
                <div key={family} className="border-line border-t pt-2">
                  <Toggle
                    label={FAMILY_NAMES[family] ?? family}
                    checked={market.enabled}
                    onChange={(value) => edit((item) => void (item.enabled = value))}
                  />
                  <div className="grid grid-cols-2 gap-2">
                    <label className="text-text-2 text-xs">
                      Lowest probability
                      <input
                        type="number"
                        step="0.01"
                        min={0}
                        max={1}
                        value={market.min_prob ?? ""}
                        placeholder="None"
                        onChange={(event) =>
                          edit((item) => void (item.min_prob = parseNumber(event.target.value)))
                        }
                        className={`${INPUT} mt-1`}
                      />
                    </label>
                    <label className="text-text-2 text-xs">
                      Lowest edge
                      <input
                        type="number"
                        step="0.005"
                        min={-1}
                        max={1}
                        value={market.min_edge ?? ""}
                        placeholder="None"
                        onChange={(event) =>
                          edit((item) => void (item.min_edge = parseNumber(event.target.value)))
                        }
                        className={`${INPUT} mt-1`}
                      />
                    </label>
                  </div>
                  {market.min_prob === null && (
                    <p className="text-muted mt-1 text-xs">
                      No threshold, so this market makes no legs.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}

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
