/**
 * Tyre panel logic (Phase 6F), pure and unit-tested: which state the panel is in, how numbers are
 * worded, where the pit window is shaded, and which "as of" laps the slider offers.
 *
 * The forecast only ever uses laps ≤ "as of" (the API's cut rule). Nothing here reads a lap after
 * it: the chart and the summary are built from the response for that lap only.
 */
import { ApiError, type Schemas } from "./api/client";
import { MISSING } from "./format";

export type TirePrediction = Schemas["TirePrediction"];
export type TirePitWindow = Schemas["PitWindow"];
export type StintSummary = Schemas["StintSummary"];
export type DriverStints = Schemas["DriverStints"];

// ---------------------------------------------------------------- state selection
export type TireView =
  | { kind: "loading" }
  | { kind: "ok"; prediction: TirePrediction; wet: boolean }
  | { kind: "insufficient"; prediction: TirePrediction; used: number; required: number }
  | { kind: "wet"; prediction: TirePrediction }
  | { kind: "model_unavailable"; message: string; requestId: string | null }
  | { kind: "non_race"; message: string }
  | { kind: "error"; error: Error; code: string; requestId: string | null };

/** API codes the panel shows as a state of its own (no error toast). */
export const HANDLED_CODES = ["model_not_available", "model_checksum_mismatch", "not_a_race_session"] as const;

export function tireViewState(query: { isPending: boolean; data?: TirePrediction; error?: Error | null }): TireView {
  if (query.error) {
    const api = query.error instanceof ApiError ? query.error : null;
    if (api && (api.code === "model_not_available" || api.code === "model_checksum_mismatch")) {
      return { kind: "model_unavailable", message: api.message, requestId: api.requestId };
    }
    if (api?.code === "not_a_race_session") return { kind: "non_race", message: api.message };
    return { kind: "error", error: query.error, code: api?.code ?? "client_error", requestId: api?.requestId ?? null };
  }
  const p = query.data;
  if (query.isPending || !p) return { kind: "loading" };
  if (p.status === "insufficient_data") {
    if (p.reason_code === "wet_compound") return { kind: "wet", prediction: p };
    return { kind: "insufficient", prediction: p, used: p.clean_laps_used ?? 0, required: p.clean_laps_required };
  }
  return { kind: "ok", prediction: p, wet: Boolean(p.rainfall_in_stint) };
}

// ---------------------------------------------------------------- formatting
function fixed(value: number, digits: number): string {
  const s = value.toFixed(digits);
  return s.startsWith("-") ? `−${s.slice(1)}` : s;
}

/** "0.090 s/lap, 95% CI 0.050–0.130"; the CI is omitted (never invented) when absent. */
export function formatDegradation(value: number | null | undefined, ci: number[] | null | undefined, digits = 3): string {
  if (value == null || !Number.isFinite(value)) return MISSING;
  const head = `${fixed(value, digits)} s/lap`;
  if (!ci || ci.length !== 2 || !ci.every(Number.isFinite)) return head;
  return `${head}, 95% CI ${fixed(ci[0]!, digits)}–${fixed(ci[1]!, digits)}`;
}

export const DEGRADATION_TOOLTIP =
  "A combined adjusted trend: how much slower each lap gets as the tyre ages, after removing the "
  + "fuel effect (the car gets lighter). It still includes track evolution and anything else that "
  + "changes with the stint, so it is not pure tyre wear.";

export function formatWindow(w: TirePitWindow | null | undefined): string {
  if (!w) return MISSING;
  const lap = (n: number | null | undefined) => (n == null ? null : `lap ${n}`);
  if (w.earliest_lap == null) return "No crossing within the forecast horizon";
  const parts = [`earliest ${lap(w.earliest_lap)}`, `central ${lap(w.central_lap) ?? "beyond the horizon"}`,
    `latest ${lap(w.latest_lap) ?? "beyond the horizon"}`];
  return parts.join(" · ");
}

export function clampCompound(compound: string | null | undefined): Compound {
  const c = (compound ?? "").toUpperCase();
  return (c in COMPOUNDS ? c : "UNKNOWN") as Compound;
}

// ---------------------------------------------------------------- compound convention
export const COMPOUNDS = {
  SOFT: { letter: "S", label: "Soft", color: "#da291c" },
  MEDIUM: { letter: "M", label: "Medium", color: "#ffd12e" },
  HARD: { letter: "H", label: "Hard", color: "#f0f0ec" },
  INTERMEDIATE: { letter: "I", label: "Intermediate", color: "#43b02a" },
  WET: { letter: "W", label: "Wet", color: "#0067ad" },
  UNKNOWN: { letter: "?", label: "Unknown", color: "#8f8a86" },
} as const;
export type Compound = keyof typeof COMPOUNDS;

// ---------------------------------------------------------------- pit-window geometry
export interface WindowShading {
  x0: number;               // first shaded lap (earliest), drawn from its left edge
  x1: number;               // last shaded lap: latest, or the horizon end when open-ended
  central: number | null;
  openEnded: boolean;
}

/** Shaded range for the chart: [earliest − ½, end + ½] in lap units; null when there is no window. */
export function windowShading(w: TirePitWindow | null | undefined, horizonEndLap: number | null): WindowShading | null {
  if (!w || w.earliest_lap == null) return null;
  const openEnded = w.latest_lap == null;
  const end = openEnded ? horizonEndLap ?? w.central_lap ?? w.earliest_lap : w.latest_lap!;
  return { x0: w.earliest_lap - 0.5, x1: end + 0.5, central: w.central_lap ?? null, openEnded };
}

// ---------------------------------------------------------------- stints and the "as of" slider
export function stintForLap(stints: StintSummary[], lap: number): StintSummary | null {
  return stints.find((s) => s.first_lap <= lap && lap <= s.last_lap) ?? null;
}

/** "As of" when a stint is selected: its last lap, but never the race's final lap (nothing left to forecast). */
export function defaultAsOf(stint: StintSummary, raceLaps: number | null): number {
  const last = raceLaps ? Math.min(stint.last_lap, raceLaps - 1) : stint.last_lap;
  return Math.max(stint.first_lap, last);
}

/**
 * Slider range: laps of the stint once enough clean laps exist. A stint's clean laps are counted
 * from its start; with the out-lap usually excluded, the earliest forecastable lap is
 * first_lap + required (the response confirms it, and the panel shows insufficient_data otherwise).
 * Null when the stint never reaches the requirement.
 */
export function asOfBounds(stint: StintSummary, required: number, raceLaps: number | null): [number, number] | null {
  if (stint.clean_laps < required) return null;
  const hi = raceLaps ? Math.min(stint.last_lap, raceLaps - 1) : stint.last_lap;
  const lo = stint.first_lap + required;  // lap 1 / the out-lap is excluded, then `required` clean laps
  return lo <= hi ? [lo, hi] : null;
}

// ---------------------------------------------------------------- chart text summary (a11y)
export function chartSummary(p: TirePrediction): string {
  const hist = p.history ?? [];
  const clean = hist.filter((h) => h.clean);
  const pred = p.predicted_laps ?? [];
  const parts = [
    `As of lap ${p.lap}: ${clean.length} clean and ${hist.length - clean.length} excluded laps observed in stint ${p.stint ?? MISSING}`
      + (hist.length ? ` (laps ${hist[0]!.lap}–${hist[hist.length - 1]!.lap})` : "") + ".",
  ];
  if (pred.length) {
    const a = pred[0]!;
    const z = pred[pred.length - 1]!;
    parts.push(`Forecast lap ${a.lap}: ${a.pred_s.toFixed(3)} s (80% range ${a.lo_s.toFixed(3)}–${a.hi_s.toFixed(3)}); `
      + `lap ${z.lap}: ${z.pred_s.toFixed(3)} s (${z.lo_s.toFixed(3)}–${z.hi_s.toFixed(3)}).`);
  }
  if (p.pit_window) parts.push(`Pit window (heuristic): ${formatWindow(p.pit_window)}; confidence ${p.pit_window.confidence}.`);
  else if (p.pit_window_reason) parts.push(`No pit window: ${p.pit_window_reason.replace(/_/g, " ")}.`);
  return parts.join(" ");
}
