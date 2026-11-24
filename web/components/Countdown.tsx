"use client";

import { useEffect, useState } from "react";

import { countdown } from "@/lib/format";

/** Ticks every 30 seconds: the earliest kickoff decides when a ticket stops being bookable. */
export function Countdown({ to }: { to: string | null | undefined }) {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(timer);
  }, []);
  // Rendered on the server without a clock, so the first paint can't disagree with the browser.
  return <span className="num">{now ? countdown(to, now) : "…"}</span>;
}
