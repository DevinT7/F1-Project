/**
 * Phase 7F end-to-end safety test: the real backend runs with RACEIQ_ENGINEER_FAKE_LLM=inject, a
 * scripted LLM whose every report and answer carries an ungrounded number ("0.35 s") and a
 * fabricated cause ("tyre wear"). The validator must reject them, so the DOM never shows either:
 * the user sees the validated template, badged "Template summary" with the reason.
 */
import { expect, test, type Page } from "@playwright/test";

const URL = "/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest";
// the injected sentence is "The driver lost 0.35 s in Turn 1 because of tyre wear." The corner panel can show a
// real "0.35 s" (Turn 16 full-throttle time), so the page-wide check looks for the injected phrases and the
// engineer section is held to the stricter "no 0.35 at all".
const INJECTED = ["lost 0.35 s", "tyre wear", "because of"];

async function assertClean(page: Page) {
  const text = await page.locator("body").innerText();
  for (const bad of INJECTED) expect(text, `"${bad}" reached the DOM`).not.toContain(bad);
  // the AI Engineer tab (report and chat) is held to the stricter "no 0.35 at all"
  for (const id of ["#engineer"]) {
    if (await page.locator(id).count()) expect(await page.locator(id).innerText()).not.toMatch(/\b0\.35\b/);
  }
}

test("injected numbers and causes never reach the DOM (report, popovers and chat)", async ({ page }) => {
  await page.goto(URL);
  await page.getByRole("tab", { name: "AI Engineer" }).click();
  const report = page.getByTestId("engineer-report");
  await expect(report).toBeVisible({ timeout: 60_000 });

  // the LLM draft was rejected: validated template, with the reason visible
  await expect(page.getByTestId("engineer-badge").first()).toContainText("Template summary");
  await expect(page.getByTestId("engineer-badge").first()).toContainText("unsupported_causal_claim");
  await expect(page.getByTestId("engineer-fallback-note")).toContainText("failed the fact check");
  await expect(report).toContainText("ALO was 0.488 s slower than VER");
  await assertClean(page);

  // every fact popover too
  const indicators = report.getByTestId("fact-indicator");
  const n = await indicators.count();
  for (let i = 0; i < n; i++) {
    await indicators.nth(i).focus();
    await expect(page.getByTestId("fact-popover")).toBeVisible();
    await assertClean(page);
  }

  // the chat: a typed question and a suggestion chip
  await page.getByTestId("engineer-input").fill("Where did ALO lose the most time?");
  await page.getByTestId("engineer-input").press("Enter");
  await expect(page.getByTestId("engineer-answer").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("engineer-chip").first().click();
  await expect(page.getByTestId("engineer-answer")).toHaveCount(2, { timeout: 30_000 });
  await assertClean(page);

  // out of scope: refused without an LLM call, styled neutrally
  await page.getByTestId("engineer-input").fill("Who will win the championship?");
  await page.getByTestId("engineer-input").press("Enter");
  await expect(page.getByTestId("engineer-answer").nth(2)).toHaveAttribute("data-variant", "neutral");
  await assertClean(page);
});

test("clicking a report bullet opens its corner on the Track Map tab", async ({ page }) => {
  await page.goto(URL);
  await page.getByRole("tab", { name: "AI Engineer" }).click();
  const report = page.getByTestId("engineer-report");
  await expect(report).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("engineer-footer")).toHaveText(
    "Numbers are computed by the analysis engine; the AI only explains them.");
  await report.getByRole("button", { name: /^Turn 16:/ }).click();
  await expect(page).toHaveURL(/tab=track/);
  await expect(page.getByTestId("corner-panel").getByRole("heading", { level: 3 })).toHaveText("Turn 16");
});
