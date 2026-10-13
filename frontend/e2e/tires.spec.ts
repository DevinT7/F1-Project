/**
 * E2E (Phase 6F): real backend on the fixture repo, synthetic race 2024 R99 (AAA's laps from 41 on
 * are ~4 s slower: "distinctive later laps"). Open the race → choose a driver and stint → the panel
 * shows degradation and the window → slide "as of" and the forecast changes, never showing a later lap.
 */
import { expect, test, type Page } from "@playwright/test";

const DISTINCTIVE_S = 94;

async function observedTrace(page: Page): Promise<{ x: number[]; y: number[] }> {
  return page.evaluate(() => {
    const gd = document.querySelector("[data-testid=tire-chart] .js-plotly-plot") as unknown as
      { data: { name: string; x: number[]; y: number[] }[] };
    const t = gd.data.find((d) => d.name === "Observed clean laps")!;
    return { x: [...t.x], y: [...t.y] };
  });
}

test("race → driver/stint → degradation and window → as-of slider changes the forecast", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(e.message));

  await page.goto("/?s=2024-99-R");
  const section = page.getByTestId("tire-section");
  await expect(section).toBeVisible({ timeout: 30_000 });
  await section.getByLabel("Driver").selectOption("AAA");
  await section.getByTestId("stint-2").click();
  await expect(page).toHaveURL(/tyre=AAA&asof=49/);

  const ok = section.getByTestId("tire-state-ok");
  await expect(ok.getByTestId("degradation-value")).toContainText("s/lap, 95% CI");
  await expect(ok.locator(".js-plotly-plot")).toBeVisible({ timeout: 30_000 });

  // as of lap 40: a window, and nothing from the distinctive laps 41+
  await section.getByTestId("as-of-slider").fill("40");
  await expect(section.getByTestId("as-of-value")).toHaveText("40");
  await expect(section.getByTestId("pit-window-central")).toHaveText("Lap 48");
  await expect(section.getByTestId("pit-window-text")).toContainText("earliest lap 41");
  let obs = await observedTrace(page);
  expect(Math.max(...obs.x)).toBe(40);
  expect(Math.max(...obs.y)).toBeLessThan(DISTINCTIVE_S);

  // slide back to 35: the forecast changes (no window any more) and still nothing after 35
  await section.getByTestId("as-of-slider").fill("35");
  await expect(section.getByTestId("as-of-value")).toHaveText("35");
  await expect(section.getByTestId("pit-window-text")).toContainText("No window");
  await expect.poll(async () => Math.max(...(await observedTrace(page)).x)).toBe(35);
  obs = await observedTrace(page);
  expect(Math.max(...obs.y)).toBeLessThan(DISTINCTIVE_S);
  await expect(page.getByTestId("tire-chart-summary")).toContainText("As of lap 35");

  expect(consoleErrors).toEqual([]);
});

test("a stint without enough clean laps shows the progress message", async ({ page }) => {
  await page.goto("/?s=2024-99-R&tyre=BBB");
  const section = page.getByTestId("tire-section");
  await section.getByTestId("stint-3").click();
  await expect(section.getByTestId("clean-lap-progress")).toHaveText("2/5 clean laps");
  await expect(section.getByTestId("tire-state-insufficient")).toContainText("A forecast needs at least 5 clean laps");
});
