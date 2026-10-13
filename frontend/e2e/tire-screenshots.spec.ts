/**
 * Screenshots of the tyre panel's states for docs/screenshots/tires/ (Phase 6F acceptance).
 * Opt-in, so ordinary e2e runs never rewrite binaries:
 *
 *     SCREENSHOTS=1 npx playwright test tire-screenshots
 *
 * Real backend on the fixture repo. The 503 state replays the API's own exported 503 response
 * (src/fixtures/api/tires_error_model_unavailable.json); everything else is live.
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const OUT = path.resolve(__dirname, "../../docs/screenshots/tires");
const NO_MODEL = readFileSync(path.resolve(__dirname, "../src/fixtures/api/tires_error_model_unavailable.json"), "utf-8");

test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to regenerate docs/screenshots/tires");

async function shoot(page: Page, name: string) {
  const section = page.getByTestId("tire-section");
  await section.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400); // let Plotly finish its transition
  await section.screenshot({ path: path.join(OUT, `${name}.png`) });
}

test("ok", async ({ page }) => {
  await page.goto("/?s=2024-99-R&tyre=AAA&asof=40");
  await expect(page.getByTestId("tire-chart").locator(".js-plotly-plot")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "1-ok");
});

test("insufficient data", async ({ page }) => {
  await page.goto("/?s=2024-99-R&tyre=BBB&asof=49");
  await expect(page.getByTestId("clean-lap-progress")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "2-insufficient-data");
});

test("model unavailable", async ({ page }) => {
  await page.route("**/api/v1/tires/predict**", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: NO_MODEL,
      headers: { "X-Request-ID": "fixture-tires_error_model_unavailable" } }));
  await page.goto("/?s=2024-99-R&tyre=AAA&asof=40");
  await expect(page.getByTestId("tire-state-model-unavailable")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "3-model-unavailable");
});

test("non-race session", async ({ page }) => {
  await page.goto("/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest&tyre=VER");
  await expect(page.getByTestId("tire-state-non-race")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("qualifying-tire-chart").locator(".js-plotly-plot")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "4-non-race");
});

test("wet conditions", async ({ page }) => {
  await page.goto("/?s=2024-99-R&tyre=CCC&asof=8");
  await expect(page.getByTestId("tire-wet-notice")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "5-wet");
});

test("loading", async ({ page }) => {
  await page.route("**/api/v1/tires/predict**", () => { /* never answer */ });
  await page.goto("/?s=2024-99-R&tyre=AAA&asof=40");
  await expect(page.getByTestId("tire-state-loading")).toBeVisible({ timeout: 60_000 });
  await shoot(page, "0-loading");
});
