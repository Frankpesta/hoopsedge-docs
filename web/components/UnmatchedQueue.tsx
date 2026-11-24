"use client";

import { useEffect, useState } from "react";

import type { Sport, Team, UnmatchedItem } from "@/lib/types";

import { UnmatchedCard } from "./UnmatchedCard";
import { Empty } from "./ui";

/** A resolved or ignored event leaves the queue as soon as its answer comes back. */
export function UnmatchedQueue({
  items,
  teams,
}: {
  items: readonly UnmatchedItem[];
  teams: Partial<Record<Sport, readonly Team[]>>;
}) {
  const [open, setOpen] = useState(items);
  useEffect(() => setOpen(items), [items]);
  if (!open.length) {
    return (
      <Empty title="Nothing to match">Every SportyBet event on recent slates found its game.</Empty>
    );
  }
  return (
    <div className="space-y-4">
      {open.map((item) => (
        <UnmatchedCard
          key={item.event_id}
          item={item}
          teams={teams[item.sport] ?? []}
          onResolved={() =>
            setOpen((current) => current.filter((other) => other.event_id !== item.event_id))
          }
        />
      ))}
    </div>
  );
}
