/**
 * Phase 6F tyre panel through the real dashboard + MSW (real API responses for the synthetic race
 * 2024-99-R; see backend/scripts/build_synthetic_race_fixture.py). Plotly is stubbed; the chart's
 * text summary (its aria-describedby) is what these tests read.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import noModel from "@/fixtures/api/tires_error_model_unavailable.json";
import { useUiStore } from "@/lib/store";
import type { TirePrediction } from "@/lib/tires";
import { apiError, server } from "@/test/msw";
import { navigation } from "@/test/render";

vi.mock("next/navigation", async () => (await import("@/test/render")).nextNavigationMock);
vi.mock("@/components/telemetry-stack", () => ({ TelemetryStack: () => <section data-testid="telemetry-plot" /> }));
vi.mock("@/components/tire-chart", () => ({
  TireChart: ({ prediction, describedBy }: { prediction: { lap: number }; describedBy: string }) =>
    <div data-testid="tire-chart" aria-describedby={describedBy}>chart as of {prediction.lap}</div>,
}));

const { renderDashboard } = await import("@/test/render");
const RACE = "s=2024-99-R";
const DISTINCTIVE_S = 94; // AAA's laps from 41 on are ~4 s slower: ≥ 94 s; everything earlier is < 92.5 s
const tires = import.meta.glob("@/fixtures/api/tires_AAA_*.json", { eager: true, import: "default" }) as Record<string, TirePrediction>;

beforeEach(() => {
  useUiStore.setState({ toasts: [] });
});

async function panel() {
  return screen.findByTestId("tire-section");
}

describe("TirePanel states", () => {
  it("ok: degradation with CI, pit window as text, confidence reasons, assumptions, model footer", async () => {
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    const ok = await screen.findByTestId("tire-state-ok");
    expect(within(ok).getByTestId("degradation-value")).toHaveTextContent(/^0\.034 s\/lap, 95% CI 0\.029–0\.039$/);
    expect(within(ok).getByTestId("pit-window-central")).toHaveTextContent("Lap 48");
    expect(within(ok).getByTestId("pit-window-text")).toHaveTextContent("earliest lap 41 · central lap 48 · latest beyond the horizon");
    expect(within(ok).getByTestId("confidence")).toHaveTextContent("low");
    expect(within(ok).getByTestId("assumptions")).toHaveTextContent("ignores safety-car probability");
    expect(within(ok).getByTestId("model-footer")).toHaveTextContent(/baseline_b_recent_conformal · version .* · trained through 2024-07-28/);
    const chart = within(ok).getByTestId("tire-chart");
    const summary = document.getElementById(chart.getAttribute("aria-describedby")!)!;
    expect(summary).toHaveTextContent("As of lap 40");
    expect(summary).toHaveTextContent("Pit window (heuristic): earliest lap 41");
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("insufficient_data explains the requirement and shows progress", async () => {
    await renderDashboard(`${RACE}&tyre=BBB&asof=49`);
    const state = await screen.findByTestId("tire-state-insufficient");
    expect(within(state).getByTestId("clean-lap-progress")).toHaveTextContent("2/5 clean laps");
    expect(state).toHaveTextContent("A forecast needs at least 5 clean laps in the current stint");
    expect(screen.queryByTestId("pit-window-card")).toBeNull();
  });

  it("wet tyres get the wet-conditions notice and no forecast", async () => {
    await renderDashboard(`${RACE}&tyre=CCC&asof=8`);
    const state = await screen.findByTestId("tire-state-wet");
    expect(within(state).getByTestId("tire-wet-notice")).toHaveTextContent("Intermediate tyres are not modelled");
    expect(screen.queryByTestId("tire-chart")).toBeNull();
  });

  it("model unavailable (503) is a panel state with the request id, not a toast", async () => {
    server.use(http.get("*/api/v1/tires/predict", () =>
      HttpResponse.json(noModel, { status: 503, headers: { "X-Request-ID": noModel.error.request_id } })));
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    const state = await screen.findByTestId("tire-state-model-unavailable");
    expect(state).toHaveTextContent("Tyre model unavailable");
    expect(state).toHaveTextContent("request_id: fixture-tires_error_model_unavailable");
    expect(screen.getByTestId("stint-timeline")).toBeInTheDocument(); // stint data still shown
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("non-race session: notice, raw stint data and the qualifying chart; no forecast is requested", async () => {
    const predict = vi.fn();
    server.use(http.get("*/api/v1/tires/predict", () => { predict(); return HttpResponse.json({}); }));
    await renderDashboard("s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest&tyre=VER");
    const section = await panel();
    expect(await within(section).findByTestId("tire-state-non-race")).toHaveTextContent(
      "Tire forecasting requires a race; showing raw stint data.");
    expect(await within(section).findByTestId("stint-timeline")).toBeInTheDocument();
    const q = within(section).getByTestId("qualifying-tire-chart");
    expect(q).toHaveTextContent("Observed laps — not a prediction");
    expect(predict).not.toHaveBeenCalled();
  });

  it("other errors: panel error with code and request id, plus the global toast", async () => {
    server.use(http.get("*/api/v1/tires/predict", () => apiError(500, "internal_error", "boom", "req-tire-500")));
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    const state = await screen.findByTestId("tire-state-error");
    expect(state).toHaveTextContent("request_id: req-tire-500");
    await waitFor(() => expect(useUiStore.getState().toasts.some((t) => t.requestId === "req-tire-500")).toBe(true));
  });

  it("loading skeleton while the forecast is computed", async () => {
    server.use(http.get("*/api/v1/tires/predict", async () => { await delay("infinite"); return HttpResponse.json({}); }));
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    expect(await screen.findByTestId("tire-state-loading")).toHaveAttribute("role", "status");
  });
});

describe("stints and the as-of slider", () => {
  it("clicking a stint selects it and its last lap", async () => {
    const user = userEvent.setup();
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    await screen.findByTestId("tire-state-ok");
    expect(screen.getByTestId("stint-2")).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByTestId("stint-1"));
    await waitFor(() => expect(navigation.search).toContain("asof=22"));
    expect(screen.getByTestId("stint-1")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("stint-1")).toHaveAccessibleName(/Stint 1: Medium, laps 1–22/);
  });

  it("sliding as-of changes the forecast and never shows a later lap", async () => {
    await renderDashboard(`${RACE}&tyre=AAA&asof=40`);
    await screen.findByTestId("tire-state-ok");
    const slider = screen.getByTestId("as-of-slider");
    expect(slider).toHaveAttribute("min", "28");
    expect(slider).toHaveAttribute("max", "49");
    fireEvent.change(slider, { target: { value: "35" } });
    await waitFor(() => expect(screen.getByTestId("as-of-value")).toHaveTextContent("35"));
    await waitFor(() => expect(screen.getByTestId("pit-window-text")).toHaveTextContent("No window: forecast uncertainty too high"));
    const observed = screen.getByTestId("observed-laps");
    const laps = [...observed.textContent!.matchAll(/Lap (\d+): (\d+):(\d+\.\d+)/g)]
      .map((m) => ({ lap: Number(m[1]), s: Number(m[2]) * 60 + Number(m[3]) }));
    expect(Math.max(...laps.map((l) => l.lap))).toBe(35);
    expect(laps.every((l) => l.s < DISTINCTIVE_S || l.lap === 23)).toBe(true); // lap 23 = out-lap, excluded
  });
});

describe("the cut rule on every exported as-of lap (distinctive later laps)", () => {
  it("no response for as-of N contains a lap after N or a distinctive lap time when N ≤ 40", () => {
    const entries = Object.entries(tires).map(([path, body]) => [Number(path.match(/AAA_(\d+)/)![1]), body] as const);
    expect(entries.length).toBe(25);
    for (const [asof, p] of entries) {
      expect(p.lap).toBe(asof);
      expect(Math.max(...p.history!.map((h) => h.lap))).toBe(asof);
      if (asof <= 40) {
        const clean = p.history!.filter((h) => h.clean).map((h) => h.lap_time_s!);
        expect(Math.max(...clean)).toBeLessThan(DISTINCTIVE_S);
        expect(Math.max(...(p.predicted_laps ?? []).map((q) => q.pred_s))).toBeLessThan(DISTINCTIVE_S);
      }
    }
  });
});
