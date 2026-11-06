import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/** Docs/10 requires WCAG AA. Tokens are the only source of colour, so test them directly. */
const css = readFileSync("app/globals.css", "utf-8");

function tokens(block: string): Record<string, string> {
  const found: Record<string, string> = {};
  for (const match of block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/g)) {
    found[match[1] as string] = match[2] as string;
  }
  return found;
}

const dark = tokens(css.slice(css.indexOf(":root {"), css.indexOf(':root[data-theme="light"]')));
const light = tokens(css.slice(css.indexOf(':root[data-theme="light"]'), css.indexOf("@theme")));

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16) / 255);
  const [r, g, b] = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  ) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(one: string, two: string): number {
  const [high, low] = [luminance(one), luminance(two)].sort((a, b) => b - a) as [number, number];
  return (high + 0.05) / (low + 0.05);
}

const TEXT_PAIRS = [
  ["text", "ink"],
  ["text", "surface"],
  ["text", "surface-2"],
  ["text-2", "ink"],
  ["text-2", "surface"],
  ["text-2", "surface-2"],
  ["muted", "surface"],
  ["muted", "ink"],
  ["amber", "surface"],
  ["amber", "ink"],
  ["on-amber", "amber"],
  ["loss", "surface"],
  ["loss", "ink"],
] as const;

describe.each([
  ["dark", dark],
  ["light", light],
])("the %s theme", (_, theme) => {
  it.each(TEXT_PAIRS)("%s on %s reaches AA for text", (foreground, background) => {
    expect(ratio(theme[foreground] as string, theme[background] as string)).toBeGreaterThanOrEqual(
      4.5,
    );
  });

  it.each(["series-1", "series-2", "series-3"])("%s stands out as a chart mark", (series) => {
    expect(ratio(theme[series] as string, theme.surface as string)).toBeGreaterThanOrEqual(3);
  });
});
