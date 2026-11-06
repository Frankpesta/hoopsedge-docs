import { apiGet } from "@/lib/api";
import type { SystemView } from "@/lib/types";

/** Loud on purpose: nobody should mistake a practice code for a real one, or a paused system. */
export async function Banners() {
  let system: SystemView;
  try {
    system = await apiGet<SystemView>("/system");
  } catch {
    return null; // The page itself explains a lost connection.
  }
  return (
    <>
      {system.dry_run && (
        <p
          role="status"
          className="bg-amber text-on-amber px-4 py-1.5 text-center text-sm font-semibold"
        >
          Dry run is on: codes here are practice codes and don&apos;t exist on SportyBet.
        </p>
      )}
      {!system.dry_run && system.scheduled_dry_run && (
        <p role="status" className="bg-surface-2 text-text px-4 py-1.5 text-center text-sm">
          Run now books real SportyBet codes. Scheduled runs make practice codes.
        </p>
      )}
      {system.pause_all && (
        <p role="status" className="bg-loss px-4 py-1.5 text-center text-sm font-semibold text-ink">
          Paused: scheduled runs are off until you turn them back on.
        </p>
      )}
    </>
  );
}
