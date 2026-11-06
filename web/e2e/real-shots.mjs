// Pictures of the running dashboard on real data, and a check that each page loads.
//   docker compose --profile manual run --rm --no-deps web-e2e node e2e/real-shots.mjs
import { chromium } from "@playwright/test";

const BASE = process.env.DASHBOARD_URL ?? "http://web:3000";
const PAGES = [
  ["real-today", "/"],
  ["real-record", "/track-record"],
  ["real-models", "/models"],
  ["real-runs", "/runs"],
  ["real-settings", "/settings"],
  ["real-system", "/system"],
];

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 375, height: 760 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
});
const page = await context.newPage();
const problems = [];
page.on("pageerror", (error) => problems.push(`${page.url()}: ${error.message}`));
page.on("console", (message) => {
  if (message.type() === "error") problems.push(`${page.url()}: ${message.text()}`);
});

async function shoot(name) {
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `test-results/shots/${name}.png`, fullPage: true });
}

for (const [name, path] of PAGES) {
  const response = await page.goto(`${BASE}${path}`);
  console.log(path, response?.status());
  await shoot(name);
}

// A real ticket and game, reached the way the owner would.
await page.goto(`${BASE}/`);
const tab = page.getByRole("tab", { name: "NCAA" });
await tab.click();
const full = page.getByRole("link", { name: "Full ticket" }).first();
if (await full.count()) {
  await page
    .getByRole("button", { name: /^Show \d+ legs$/ })
    .first()
    .click();
  await shoot("real-today-open");
  const text = await page.locator("article").first().innerText();
  console.log("first ticket says 1 in?", /1 in/.test(text), "| Model?", /Model \d/.test(text));
  const game = page.locator('article a[href^="/games/"]').first();
  const gameHref = await game.getAttribute("href");
  await full.click();
  await page.waitForURL(/tickets/);
  await shoot("real-ticket");
  if (gameHref) {
    const response = await page.goto(`${BASE}${gameHref}`);
    console.log(gameHref, response?.status());
    await shoot("real-game");
  }
}
console.log(problems.length ? `PROBLEMS:\n${problems.join("\n")}` : "No page or console errors.");
await browser.close();
