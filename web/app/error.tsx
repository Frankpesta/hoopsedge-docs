"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="border-loss mt-8 rounded-lg border p-6 text-center">
      <h1 className="font-display text-3xl font-extrabold uppercase">Can&apos;t load this page</h1>
      <p className="text-text-2 mt-2 text-sm">
        The dashboard couldn&apos;t reach the API. Your tickets and data aren&apos;t affected.
      </p>
      <button
        type="button"
        onClick={reset}
        className="bg-amber text-on-amber mt-4 min-h-11 rounded-md px-5 font-semibold"
      >
        Try again
      </button>
    </div>
  );
}
