import { expect, test } from "@playwright/test";

const PAGES = [
  "/",
  "/tickets/handicap-1",
  "/tickets/half-1",
  "/games/g1",
  "/track-record",
  "/models",
  "/unmatched",
  "/runs",
  "/runs/run-NCAA",
  "/settings",
  "/system",
];

async function openNcaa(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.getByRole("tab", { name: "NCAA" }).click();
}

test("copying a code puts it on the clipboard", async ({ page }) => {
  await openNcaa(page);
  await page.getByRole("button", { name: /H4NDC4/ }).click();
  await expect(page.getByText("Copied", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("H4NDC4");
});

test("switching markets shows that market's tickets", async ({ page }) => {
  await openNcaa(page);
  await expect(page.getByRole("button", { name: /H4NDC4/ })).toBeVisible();
  await page.getByRole("tab", { name: /HT O\/U/ }).click();
  await expect(page.getByRole("button", { name: /DRYRUN-ABC123/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /H4NDC4/ })).toBeHidden();
  await page.getByRole("tab", { name: /Team O\/U/ }).click();
  await expect(page.getByText("No Team O/U tickets on this slate")).toBeVisible();
});

test("an unproven ticket shows no probability anywhere (D72)", async ({ page }) => {
  await openNcaa(page);
  await page.getByRole("tab", { name: /HT O\/U/ }).click();
  await page.getByRole("button", { name: "Show 2 legs" }).click();
  const card = page.locator("article").first();
  await expect(card.getByText(/Treat each leg as about 50%/)).toBeVisible();
  await expect(card.getByText("Unproven", { exact: true })).toBeVisible();
  await expect(card).not.toContainText("1 in");
  await expect(card).not.toContainText("Model ");
  await expect(card.getByText(/failed its evidence gate/)).toBeVisible();
  await expect(card.getByText(/Practice code/)).toBeVisible();
});

test("an invalid code is struck through and can't be copied", async ({ page }) => {
  await openNcaa(page);
  await page.getByRole("tab", { name: /FT O\/U/ }).click();
  await expect(page.getByRole("button", { name: /T0TAL1/ })).toBeDisabled();
  await expect(page.getByText("1 selections missing, 0 unexpected")).toBeVisible();
});

test("a league with no run explains why it's empty", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "NBA" }).click();
  await expect(
    page.getByText(/9 games are scheduled, but this slate hasn't been run/),
  ).toBeVisible();
});

test("excluded legs show their reasons, with a link to the queue", async ({ page }) => {
  await openNcaa(page);
  await page.getByText(/^Excluded/).click();
  await expect(page.getByText("Probability below the threshold")).toBeVisible();
  await expect(page.getByRole("link", { name: /need a match/ })).toHaveAttribute(
    "href",
    "/unmatched",
  );
});

test("run now asks first, then follows the run to its end", async ({ page }) => {
  await openNcaa(page);
  await page.getByRole("button", { name: "Run NCAA now" }).click();
  await expect(page.getByRole("dialog")).toContainText("superseded");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.getByRole("button", { name: "Run NCAA now" }).click();
  await page.getByRole("button", { name: "Start the run" }).click();
  await expect(page.getByRole("button", { name: "Running" })).toBeVisible();
  // The fake finishes after a few polls, so this proves polling, not just the first response.
  await expect(page.getByRole("button", { name: "Run NCAA now" })).toBeVisible({ timeout: 30_000 });
});

test("resolving an unmatched event clears it", async ({ page }) => {
  await page.goto("/unmatched");
  await expect(page.getByRole("heading", { name: /Mystery Tech/ })).toBeVisible();
  const confirm = page.getByRole("button", { name: "Confirm match" });
  await expect(confirm).toBeDisabled();
  await page.getByRole("button", { name: "Duke Blue Devils" }).click();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByText("Nothing to match")).toBeVisible();
});

test("settings preview a change and save it", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByText("No unsaved changes")).toBeVisible();
  await page.getByLabel("Fewest legs on a ticket").fill("3");
  await expect(page.getByText("1 unsaved changes")).toBeVisible();
  await expect(page.getByText("min_legs: 2 → 3")).toBeVisible();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/Saved as version/)).toBeVisible();
});

test("the dry-run banner is loud while dry run is on", async ({ page }) => {
  await page.goto("/settings");
  await page.getByLabel(/^Dry run/).check();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/Saved as version/)).toBeVisible();
  await page.goto("/");
  await expect(page.getByText(/Dry run is on/)).toBeVisible();
  await page.goto("/settings");
  await page.getByLabel(/^Dry run/).uncheck();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/Saved as version/)).toBeVisible();
});

test("a ticket shows its leg results and history", async ({ page }) => {
  await page.goto("/tickets/handicap-1");
  await expect(page.getByText("Duke -6.5")).toBeVisible();
  await expect(page.getByText(/Result 150/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Version 2" })).toBeVisible();
});

test("a game shows its forecast, charts and a table view of them", async ({ page }) => {
  await page.goto("/games/g1");
  await expect(page.getByText("Predicted score")).toBeVisible();
  await expect(page.locator("svg.recharts-surface").first()).toBeVisible();
  await page.getByRole("button", { name: "Table" }).first().click();
  await expect(page.getByRole("cell", { name: "Duke Blue Devils covers" })).toBeVisible();
});

test("a filtered track record with nothing says so", async ({ page }) => {
  await page.goto("/track-record");
  await expect(page.getByText("52.6%", { exact: true })).toBeVisible();
  await page.getByLabel("League").selectOption("NBA");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("No settled legs yet")).toBeVisible();
});

test("a rollback asks first and needs a reason box, not a typed command", async ({ page }) => {
  await page.goto("/models");
  await page.getByRole("button", { name: "Roll back to this" }).first().click();
  await expect(page.getByRole("dialog").first()).toContainText("20261105-1000-older00");
  await page.getByRole("button", { name: "Cancel" }).first().click();
});

test("what doesn't exist gets a friendly page", async ({ page }) => {
  await page.goto("/tickets/nothing-here");
  await expect(page.getByText("Not found")).toBeVisible();
  await page.goto("/slate/not-a-date");
  await expect(page.getByText("Not found")).toBeVisible();
});

for (const path of PAGES) {
  test(`${path} fits a 375 px phone with no sideways scroll`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

test("no request from the browser carries the key or names the API", async ({ page }) => {
  const seen: string[] = [];
  page.on("request", (request) => {
    seen.push(request.url());
    expect(JSON.stringify(request.headers())).not.toContain("e2e-key");
  });
  await openNcaa(page);
  await page.goto("/settings");
  expect(seen.some((url) => url.includes(":8100"))).toBe(false);
  const html = await page.content();
  expect(html).not.toContain("e2e-key");
  expect(html).not.toContain("127.0.0.1:8100");
});
