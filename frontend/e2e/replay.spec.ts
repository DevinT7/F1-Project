/**
 * Track-map lap replay in a real browser: the dots start on the start/finish line, play moves
 * them, and clicking a corner number puts the faster driver at that corner.
 */
import { expect, test } from "@playwright/test";

const URL = "/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest";

test("lap replay: play, then click a corner number", async ({ page }) => {
  await page.goto(URL);
  await page.getByTestId("map-mode-replay").click({ timeout: 60_000 });
  const dist = async (side: string) => Number(await page.getByTestId(`replay-dot-${side}`).getAttribute("data-distance"));
  expect(await dist("reference")).toBe(0);
  expect(await dist("target")).toBe(0);

  await page.getByTestId("replay-speed").selectOption("8");
  await page.getByTestId("replay-play").click();
  await expect.poll(() => dist("reference"), { timeout: 10_000 }).toBeGreaterThan(300);
  await page.getByTestId("replay-play").click();                     // pause
  await expect(page.getByTestId("replay-status")).toContainText(/VER|ALO/);

  await page.getByTestId("map-label-hit-T6").click();
  await expect(page.getByTestId("replay-status")).toContainText(/^Turn 6: (VER|ALO) gets there first; (VER|ALO) is \d+ m back/);
  // the status is set at once, but the dots animate there over JUMP_MS (and can cross mid-flight):
  // wait until they land, one at the corner and the other exactly the reported distance back
  const back = Number(/is (\d+) m back/.exec((await page.getByTestId("replay-status").textContent()) ?? "")?.[1]);
  expect(back).toBeGreaterThan(0);
  await expect.poll(async () => Math.abs(Math.abs(await dist("reference") - await dist("target")) - back),
    { timeout: 5_000 }).toBeLessThanOrEqual(1);
  await expect(page.getByTestId("corner-panel").getByRole("heading", { level: 3 })).toHaveText("Turn 6");
  if (process.env.SCREENSHOTS) await page.locator("#track").screenshot({ path: process.env.SCREENSHOTS });
});
