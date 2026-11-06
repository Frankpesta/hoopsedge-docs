import type { Metadata, Viewport } from "next";
import { Barlow_Condensed, Inter_Tight, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import type { ReactNode } from "react";

import { Banners } from "@/components/Banners";
import { Motion } from "@/components/Motion";
import { Nav } from "@/components/Nav";
import { ThemeToggle } from "@/components/ThemeToggle";

import "./globals.css";

const display = Barlow_Condensed({
  subsets: ["latin"],
  variable: "--font-display-face",
  weight: ["600", "800"],
});
const sans = Inter_Tight({ subsets: ["latin"], variable: "--font-inter-tight" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains" });

// Every page is live data. Nothing may fetch from the API at build time, when it isn't reachable.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "HoopsEdge",
  description: "Private basketball predictions and booking codes",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#0B0D12", width: "device-width", initialScale: 1 };

// Set before the first paint, so a light-theme reader never sees a dark flash.
const themeScript = `try{var t=localStorage.getItem("theme");if(t==="light")document.documentElement.dataset.theme="light"}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${display.variable} ${sans.variable} ${mono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-dvh pb-20">
        <Motion>
          <Banners />
          <header className="mx-auto flex max-w-3xl items-center justify-between px-4 pt-4">
            <Link href="/" className="font-display text-2xl font-extrabold tracking-wide uppercase">
              Hoops<span className="text-amber">Edge</span>
            </Link>
            <ThemeToggle />
          </header>
          <main className="mx-auto max-w-3xl px-4 pt-4">{children}</main>
          <Nav />
        </Motion>
      </body>
    </html>
  );
}
