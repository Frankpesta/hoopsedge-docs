import { test } from "@playwright/test";

/** Pictures to judge the design by, not assertions. Run with SHOTS=1. */
test.skip(!process.env.SHOTS, "Only when asked for");

const SHOTS: readonly [string, string][] = [
  ["today", "/?sport=NCAAB"],
  ["ticket", "/tickets/T-lean"],
  ["teams", "/unmatched?sport=NCAAB"],
  ["runs", "/runs/run-NCAAB"],
  ["config", "/settings"],
  ["system", "/system"],
];

for (const [name, path] of SHOTS) {
  test(`shot ${name}`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(600);
    await page.screenshot({ path: `test-results/shots/${name}.png`, fullPage: true });
  });
}

test("shot light theme", async ({ page }) => {
  await page.goto("/?sport=NCAAB");
  await page.getByRole("button", { name: "Light", exact: true }).click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: "test-results/shots/today-light.png", fullPage: true });
});
