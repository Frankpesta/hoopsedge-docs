import { expect, test } from "@playwright/test";

/** The owner's daily flows at 375 px, against the mock v2 API. */

const PAGES = [
  "/",
  "/unmatched",
  "/runs",
  "/runs/run-NCAAB",
  "/settings",
  "/system",
  "/tickets/T-lean",
  "/results",
];

for (const path of PAGES) {
  test(`${path} loads without errors and fits the phone`, async ({ page }) => {
    const problems: string[] = [];
    page.on("pageerror", (error) => problems.push(error.message));
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(width).toBeLessThanOrEqual(375);
    expect(problems).toEqual([]);
  });
}

test("the slate shows tickets and the sheet for the chosen sport", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: /NCAAB/ }).click();
  await expect(page.getByRole("heading", { name: /Totals lean #1/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /H4NDC4/ })).toBeVisible();
  await expect(page.getByText("Cross-level last 5")).toBeVisible();
  await expect(page.getByRole("link", { name: "Download CSV" })).toBeVisible();
  await page.getByRole("tab", { name: /NBA/ }).click();
  await expect(page.getByText("This slate hasn't been run yet.")).toBeVisible();
});

test("copying a code puts it on the clipboard", async ({ page }) => {
  await page.goto("/?sport=NCAAB");
  await page.getByRole("button", { name: /H4NDC4/ }).click();
  await expect(page.getByText("Copied", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("H4NDC4");
});

test("superseded tickets wait under Earlier tickets", async ({ page }) => {
  await page.goto("/?sport=NCAAB");
  await expect(page.getByRole("button", { name: /OLD123/ })).toBeHidden();
  await page.getByText("Earlier tickets (1)").click();
  await expect(page.getByRole("button", { name: /OLD123/ })).toBeVisible();
  await expect(page.getByText("Superseded by a later run.")).toBeVisible();
});

test("the CSV downloads with the owner's file name", async ({ page }) => {
  await page.goto("/?sport=NCAAB");
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^ncaab_predictions_\d{8}\.csv$/);
});

test("run now asks first, then follows the run to its end", async ({ page }) => {
  await page.goto("/?sport=NCAAB");
  await page.getByRole("button", { name: "Run NCAAB now" }).click();
  await expect(page.getByRole("dialog")).toContainText("practice codes");
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Run NCAAB now" }).click();
  await page.getByRole("button", { name: "Start the run" }).click();
  await expect(page.getByRole("button", { name: "Running" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run NCAAB now" })).toBeVisible({
    timeout: 30_000,
  });
});

test("rebook says which legs will be dropped", async ({ page }) => {
  await page.goto("/tickets/T-lean");
  await page.getByRole("button", { name: "Rebook" }).click();
  await expect(page.getByRole("dialog")).toContainText("1 leg has started");
  await page.getByRole("dialog").getByRole("button", { name: "Rebook" }).click();
  await expect(page.getByText(/Rebooking/)).toBeVisible();
});

test("resolving the unmatched side saves an alias and clears the queue", async ({ page }) => {
  await page.goto("/unmatched");
  await expect(page.getByRole("heading", { name: /Mystery Tech Owls/ })).toBeVisible();
  await expect(page.getByText(/Home matched/)).toBeVisible();
  const save = page.getByRole("button", { name: "Save the alias" });
  await expect(save).toBeDisabled();
  await page.getByRole("button", { name: "Auburn Tigers" }).click();
  await save.click();
  await expect(page.getByText("Nothing to match")).toBeVisible();
});

test("an alias can be added and removed", async ({ page }) => {
  await page.goto("/unmatched?sport=NCAAB");
  await page.getByLabel(/SportyBet name/).fill("Auburn U Tigers");
  await page.getByLabel("ESPN team").fill("Auburn Tigers");
  await page.getByRole("button", { name: "Add alias" }).click();
  await expect(page.getByText("auburn u tigers")).toBeVisible();
  await page.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("auburn u tigers")).toBeHidden();
});

test("config refuses a cutoff under 20, previews a change and saves it", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByText("No unsaved changes")).toBeVisible();
  const cutoff = page.getByLabel("Stop booking, minutes before kickoff");
  await cutoff.fill("10");
  await page.getByRole("button", { name: "Save as a new version" }).click();
  // Under 20 never reaches the API: the field refuses it (invariant 2, D86).
  expect(await cutoff.evaluate((field: HTMLInputElement) => field.validity.valid)).toBe(false);
  await expect(page.getByText(/Saved as version/)).toBeHidden();
  await cutoff.fill("25");
  await expect(page.getByText("tickets.booking_cutoff_minutes: 20 → 25")).toBeVisible();
  await page.getByRole("button", { name: "Save as a new version" }).click();
  await expect(page.getByText(/Saved as version 1/)).toBeVisible();
});

test("results suggest thresholds and the owner applies one", async ({ page }) => {
  await page.goto("/results?sport=NCAAB");
  await expect(page.getByText("640")).toBeVisible();
  // D101: only the lowest-odds line is booked, so it's the only one suggested.
  await expect(page.getByRole("article").filter({ hasText: "main line" })).toHaveCount(0);
  const card = page.getByRole("article").filter({ hasText: "Game totals, lowest-odds line" });
  await expect(card.getByText("Suggested: Over 4 · Under 6.5")).toBeVisible();
  await expect(
    page.getByRole("article").filter({ hasText: "Team totals" }).getByText("Already in use."),
  ).toBeVisible();
  await card.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByRole("dialog")).toContainText("new config version");
  await page.getByRole("dialog").getByRole("button", { name: "Apply" }).click();
  await expect(page.getByRole("dialog").getByText(/Applied as config version/)).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  // The page reloads with the new version, so the suggestion is now the one in use.
  await expect(card.getByText("Already in use.")).toBeVisible();
  await expect(card.getByText("Now: Over 4 · Under 6.5")).toBeVisible();
});

test("a graded ticket shows its result and can't be rebooked", async ({ page }) => {
  await page.goto("/tickets/T-old");
  await expect(page.getByText(/^✕\s*Lost$/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Rebook" })).toBeHidden();
});

test("the dry-run banner is loud while dry run is on", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/Dry run is on/)).toBeVisible();
});

test("the system page shows the relay and alerts", async ({ page }) => {
  await page.goto("/system");
  await expect(page.getByText("Relay up")).toBeVisible();
  await expect(page.getByText("NBA run failed")).toBeVisible();
});

test("the system page says where go-live stands", async ({ page }) => {
  await page.goto("/system");
  await expect(page.getByText("3 clean practice days")).toBeVisible();
  await expect(page.getByText(/^NBA123 · /)).toBeVisible();
  await expect(page.getByText("HOOPSEDGE_SCHEDULED_DRY_RUN=false", { exact: false })).toBeVisible();
});
