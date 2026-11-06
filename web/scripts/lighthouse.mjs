// Docs/10: Lighthouse mobile performance must reach 90 on Today.
//   docker compose --profile manual run --rm --no-deps web-e2e node scripts/lighthouse.mjs
import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const url = process.env.DASHBOARD_URL ?? "http://web:3000/";
const output = "/tmp/lighthouse.json";
execFileSync(
  "npx",
  [
    "--yes",
    "lighthouse@12.8.2",
    url,
    "--quiet",
    "--form-factor=mobile",
    "--only-categories=performance,accessibility",
    "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
    "--output=json",
    `--output-path=${output}`,
  ],
  { env: { ...process.env, CHROME_PATH: chromium.executablePath() }, stdio: "ignore" },
);
const report = JSON.parse(readFileSync(output, "utf-8"));
for (const category of Object.values(report.categories)) {
  console.log(category.id, Math.round(category.score * 100));
}
for (const name of [
  "first-contentful-paint",
  "largest-contentful-paint",
  "total-blocking-time",
  "cumulative-layout-shift",
]) {
  console.log(name, report.audits[name].displayValue);
}
for (const audit of Object.values(report.audits)) {
  if (audit.score !== null && audit.score < 0.9 && audit.scoreDisplayMode !== "informative") {
    console.log("-", audit.id, audit.score, audit.displayValue ?? "");
  }
}
