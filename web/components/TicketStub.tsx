"use client";

import { m } from "framer-motion";
import { useEffect, useState } from "react";

const PRACTICE_PREFIX = "DRYRUN-";
const LONG_CODE = 8; // Real codes are short. A longer one must still fit on one line at 375 px.

/** The whole stub copies: copying codes is the daily task, so it has the biggest target there is. */
export function TicketStub({ code, status }: { code: string | null; status: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);

  if (!code) {
    return (
      <div className="stub text-text-2 relative rounded-md px-10 py-4 text-center text-sm">
        No code: this ticket wasn&apos;t booked
      </div>
    );
  }

  const invalid = status === "invalid";
  const practice = code.startsWith(PRACTICE_PREFIX);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code as string);
      setCopied(true);
    } catch {
      // Clipboard access can be refused. The code stays on screen to copy by hand.
    }
  }

  return (
    <div>
      <m.button
        type="button"
        whileTap={invalid ? undefined : { scale: 0.97 }}
        onClick={invalid ? undefined : copy}
        disabled={invalid}
        className="stub relative block min-h-16 w-full rounded-md px-10 py-3 text-center"
      >
        <span
          className={`num font-bold whitespace-nowrap ${
            code.length > LONG_CODE ? "text-lg tracking-[0.06em]" : "text-2xl tracking-[0.18em]"
          } ${invalid ? "text-loss line-through" : "text-text"}`}
        >
          {code}
        </span>
        <span className="text-muted mt-0.5 block text-xs">
          {invalid ? "Don't use this code" : copied ? "Copied" : "Tap to copy"}
        </span>
      </m.button>
      {practice && (
        <p className="text-amber mt-1 text-center text-sm font-semibold">
          Practice code: it won&apos;t load on SportyBet.
        </p>
      )}
      <span role="status" className="sr-only">
        {copied ? "Code copied" : ""}
      </span>
    </div>
  );
}
