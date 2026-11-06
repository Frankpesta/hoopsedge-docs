import { test } from "@playwright/test";

/** Pictures to judge the design by, not assertions. Run with SHOTS=1. */
test.skip(!process.env.SHOTS, "Only when asked for");

const SHOTS: readonly [string, string, string?][] = [
  ["today-handicap", "/", "NCAA"],
  ["ticket", "/tickets/handicap-1"],
  ["game", "/games/g1"],
  ["record", "/track-record"],
  ["models", "/models"],
  ["unmatched", "/unmatched"],
  ["settings", "/settings"],
  ["system", "/system"],
];

for (const [name, path, league] of SHOTS) {
  test(`shot ${name}`, async ({ page }) => {
    await page.goto(path);
    if (league) await page.getByRole("tab", { name: league }).click();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(600);
    await page.screenshot({ path: `test-results/shots/${name}.png`, fullPage: true });
  });
}

test("shot unproven ticket, open", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "NCAA" }).click();
  await page.getByRole("tab", { name: /HT O\/U/ }).click();
  await page.getByRole("button", { name: "Show 2 legs" }).click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: "test-results/shots/today-unproven.png", fullPage: true });
});

test("shot light theme", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Light", exact: true }).click();
  await page.getByRole("tab", { name: "NCAA" }).click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: "test-results/shots/today-light.png", fullPage: true });
});
