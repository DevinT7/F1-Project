/**
 * E2E smoke: real backend (Parquet fixture repo, 2024 R5 Q) + production frontend build.
 * Load a shared URL → Lap Comparison: charts render, hover syncs the map → Track Map: click a corner →
 * the panel shows its values.
 */
import { expect, test } from "@playwright/test";

const URL = "/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest";

test("shared URL → charts → hover sync → corner panel", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(e.message));

  await page.goto(URL);
  await page.getByRole("tab", { name: "Lap Comparison" }).click();
  await expect(page).toHaveURL(/tab=laps/);

  // charts render (Plotly SVG with all 11 traces)
  const plot = page.getByTestId("telemetry-plot");
  await expect(plot.locator(".main-svg").first()).toBeVisible({ timeout: 30_000 });
  await expect(plot.locator(".scatterlayer .trace")).toHaveCount(11);

  // header gap equals the delta chart's final value
  await expect(page.getByTestId("lap-gap")).toHaveText("+0.488 s");
  const deltaEnd = await page.evaluate(() => {
    const gd = document.querySelector("[data-testid=telemetry-plot] .js-plotly-plot") as unknown as { data: { name: string; y: number[] }[] };
    const d = gd.data.find((t) => t.name === "Δ time")!;
    return d.y[d.y.length - 1];
  });
  expect(deltaEnd).toBeCloseTo(0.488, 3);

  // hovering the chart moves the map marker (after the reveal animation: its clip-path also clips hit-testing)
  await plot.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished)));
  const box = (await plot.locator(".nsewdrag").first().boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.5, box.y + 40);
  await expect(page.getByTestId("map-marker")).toBeVisible();

  // Track Map tab: click a corner → the panel shows its values
  await page.getByRole("tab", { name: "Track Map" }).click();
  await expect(page.getByTestId("corner-table")).toBeVisible();
  await page.getByTestId("corner-T6").click();
  const panel = page.getByTestId("corner-panel");
  await expect(panel.getByRole("heading", { level: 3 })).toHaveText("Turn 6");
  await expect(panel.getByRole("row", { name: /Entry speed/ })).toContainText("282.0 km/h");
  await expect(panel.getByTestId("corner-delta")).toContainText("+0.098 s (slower)");  // X/Y-aligned (Phase 2 follow-up)

  expect(consoleErrors).toEqual([]);
});

test("reloading the shared URL reproduces the same comparison", async ({ page }) => {
  await page.goto(URL);
  await expect(page.getByTestId("lap-gap")).toHaveText("+0.488 s", { timeout: 30_000 });
  await page.reload();
  await expect(page.getByTestId("lap-gap")).toHaveText("+0.488 s", { timeout: 30_000 });
  await expect(page.getByLabel("Reference driver")).toHaveValue("VER");
  await expect(page.getByLabel("Target driver")).toHaveValue("ALO");
});
