"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Today" },
  { href: "/track-record", label: "Record" },
  { href: "/models", label: "Models" },
  { href: "/unmatched", label: "Unmatched" },
  { href: "/runs", label: "Runs" },
  { href: "/settings", label: "Settings" },
  { href: "/system", label: "System" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav
      aria-label="Main"
      className="bg-surface border-line fixed inset-x-0 bottom-0 z-20 border-t"
    >
      <ul className="mx-auto flex max-w-3xl">
        {LINKS.map((link) => {
          const active =
            link.href === "/"
              ? path === "/" || path.startsWith("/slate")
              : path.startsWith(link.href);
          return (
            <li key={link.href} className="flex-1">
              <Link
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`flex min-h-14 items-center justify-center px-1 text-xs whitespace-nowrap sm:text-sm ${
                  active ? "text-amber font-semibold" : "text-text-2"
                }`}
              >
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
