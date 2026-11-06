import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mt-8 text-center">
      <h1 className="font-display text-3xl font-extrabold uppercase">Not found</h1>
      <p className="text-text-2 mt-2 text-sm">
        That page doesn&apos;t exist, or it&apos;s been removed.
      </p>
      <Link href="/" className="text-amber mt-4 inline-block font-semibold underline">
        Back to today
      </Link>
    </div>
  );
}
