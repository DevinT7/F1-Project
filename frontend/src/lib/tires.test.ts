import { describe, expect, it } from "vitest";

import notRace from "@/fixtures/api/tires_error_not_race.json";
import noModel from "@/fixtures/api/tires_error_model_unavailable.json";
import ok40 from "@/fixtures/api/tires_AAA_40.json";
import ok35 from "@/fixtures/api/tires_AAA_35.json";
import short from "@/fixtures/api/tires_BBB_49.json";
import wet from "@/fixtures/api/tires_CCC_8.json";
import stints from "@/fixtures/api/race_stints.json";

import { ApiError } from "./api/client";
import {
  asOfBounds, chartSummary, defaultAsOf, formatDegradation, formatWindow, stintForLap, tireViewState,
  windowShading, type StintSummary, type TirePrediction,
} from "./tires";

const asPrediction = (x: unknown) => x as TirePrediction;
const apiError = (body: { error: { status_code: number; code: string; message: string; request_id: string } }) =>
  new ApiError(body.error.status_code, body.error.code, body.error.message, body.error.request_id);

describe("state selection (every API status)", () => {
  it("loading while pending", () => {
    expect(tireViewState({ isPending: true })).toEqual({ kind: "loading" });
  });

  it("ok, with the wet flag from rainfall_in_stint", () => {
    const v = tireViewState({ isPending: false, data: asPrediction(ok40) });
    expect(v.kind).toBe("ok");
    expect(v.kind === "ok" && v.wet).toBe(false);
    const rainy = tireViewState({ isPending: false, data: { ...asPrediction(ok40), rainfall_in_stint: true } });
    expect(rainy.kind === "ok" && rainy.wet).toBe(true);
  });

  it("insufficient_data carries progress (clean laps used / required)", () => {
    const v = tireViewState({ isPending: false, data: asPrediction(short) });
    expect(v).toMatchObject({ kind: "insufficient", used: 2, required: 5 });
  });

  it("a wet compound is its own state", () => {
    expect(tireViewState({ isPending: false, data: asPrediction(wet) }).kind).toBe("wet");
  });

  it("503 → model_unavailable, keeping the request id", () => {
    const v = tireViewState({ isPending: false, error: apiError(noModel) });
    expect(v).toMatchObject({ kind: "model_unavailable", requestId: "fixture-tires_error_model_unavailable" });
  });

  it("422 not_a_race_session → non_race", () => {
    expect(tireViewState({ isPending: false, error: apiError(notRace) }).kind).toBe("non_race");
  });

  it("anything else → error with code and request id", () => {
    const v = tireViewState({ isPending: false, error: new ApiError(500, "internal_error", "boom", "req-1") });
    expect(v).toMatchObject({ kind: "error", code: "internal_error", requestId: "req-1" });
    expect(tireViewState({ isPending: false, error: new Error("plain") })).toMatchObject({ kind: "error", code: "client_error" });
  });
});

describe("formatting", () => {
  it("degradation with its 95% CI", () => {
    expect(formatDegradation(0.09, [0.05, 0.13], 2)).toBe("0.09 s/lap, 95% CI 0.05–0.13");
    expect(formatDegradation(0.0338, [0.0289, 0.0387])).toBe("0.034 s/lap, 95% CI 0.029–0.039");
    expect(formatDegradation(-0.012, [-0.02, -0.004])).toBe("−0.012 s/lap, 95% CI −0.020–−0.004");
  });

  it("never invents a CI or a value", () => {
    expect(formatDegradation(0.05, null)).toBe("0.050 s/lap");
    expect(formatDegradation(0.05, [0.01])).toBe("0.050 s/lap");
    expect(formatDegradation(null, [0.01, 0.02])).toBe("—");
  });

  it("window as text, open-ended said plainly", () => {
    const w = asPrediction(ok40).pit_window!;
    expect(formatWindow(w)).toBe(`earliest lap ${w.earliest_lap} · central lap ${w.central_lap} · latest beyond the horizon`);
    expect(formatWindow(null)).toBe("—");
  });
});

describe("pit-window shading geometry (fixture)", () => {
  it("open-ended window shades from earliest to the horizon end, in whole-lap bands", () => {
    const p = asPrediction(ok40);
    const end = p.predicted_laps![p.predicted_laps!.length - 1]!.lap;
    const s = windowShading(p.pit_window, end)!;
    expect(s).toEqual({ x0: p.pit_window!.earliest_lap! - 0.5, x1: end + 0.5, central: p.pit_window!.central_lap, openEnded: true });
    expect(s.x0).toBeLessThan(s.central!);
    expect(s.central!).toBeLessThan(s.x1);
  });

  it("closed window shades earliest…latest", () => {
    const w = { ...asPrediction(ok40).pit_window!, earliest_lap: 30, central_lap: 32, latest_lap: 35 };
    expect(windowShading(w, 60)).toEqual({ x0: 29.5, x1: 35.5, central: 32, openEnded: false });
  });

  it("no window, or no crossing at all → nothing shaded", () => {
    expect(windowShading(asPrediction(ok35).pit_window, 60)).toBeNull();
    expect(windowShading({ ...asPrediction(ok40).pit_window!, earliest_lap: null, central_lap: null }, 60)).toBeNull();
  });
});

describe("stints and the as-of slider", () => {
  const aaa = (stints as { drivers: { driver: string; stints: StintSummary[] }[] }).drivers.find((d) => d.driver === "AAA")!.stints;
  const bbb = (stints as { drivers: { driver: string; stints: StintSummary[] }[] }).drivers.find((d) => d.driver === "BBB")!.stints;

  it("finds the stint of a lap", () => {
    expect(stintForLap(aaa, 30)?.stint).toBe(2);
    expect(stintForLap(aaa, 22)?.stint).toBe(1);
  });

  it("selecting a stint goes to its last lap, never the flag", () => {
    expect(defaultAsOf(aaa[1]!, 50)).toBe(49);
    expect(defaultAsOf(aaa[0]!, 50)).toBe(22);
  });

  it("slider starts once enough clean laps exist and is null for a stint that never gets there", () => {
    expect(asOfBounds(aaa[1]!, 5, 50)).toEqual([28, 49]);
    expect(asOfBounds(bbb[2]!, 5, 50)).toBeNull();
  });
});

describe("text summary (the chart's accessible description)", () => {
  it("states the cut and never mentions a lap after it", () => {
    const p = asPrediction(ok35);
    const text = chartSummary(p);
    expect(text).toContain("As of lap 35");
    const observed = [...text.matchAll(/laps (\d+)–(\d+)/g)].map((m) => Number(m[2]));
    expect(Math.max(...observed)).toBe(35);
  });
});
