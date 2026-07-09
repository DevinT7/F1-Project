// Mock data so Person 4 can build the UI before the backend is live.
// Shapes match the contract exactly — swap to real api.ts calls with no UI changes.
import type { LapComparison, TelemetrySample, TireAnalysis } from "./types";

function lap(offset: number): TelemetrySample[] {
  const out: TelemetrySample[] = [];
  for (let d = 0; d < 4000; d += 10) {
    const base = 320 - 180 * Math.abs(Math.sin(d / 500));
    const speed = Math.max(90, base + offset + (Math.random() - 0.5) * 4);
    out.push({
      distance: d, speed,
      throttle: Math.max(0, Math.min(100, (speed - 90) / 2.5)),
      brake: speed < 140 ? 100 : 0, n_gear: Math.min(8, Math.round(speed / 45)),
    });
  }
  return out;
}

export function mockComparison(): LapComparison {
  const you = lap(4), ref = lap(0);
  let cum = 0;
  const delta = you.map((s, i) => {
    const ta = 10 / Math.max(0.1, (s.speed! * 1000) / 3600);
    const tb = 10 / Math.max(0.1, (ref[i].speed! * 1000) / 3600);
    cum += ta - tb;
    return { distance: s.distance, delta_s: cum };
  });
  return {
    session_key: 9159, driver: { driver_number: 1, name_acronym: "VER", team_colour: "3671c6" },
    reference: { driver_number: 4, name_acronym: "NOR", team_colour: "ff8000" },
    lap_time: 73.672, reference_lap_time: 73.911,
    total_delta_s: +delta[delta.length - 1].delta_s.toFixed(3),
    time_gained_s: -0.321, time_lost_s: 0.082,
    sector_deltas: [{ sector: 1, delta_s: -0.156 }, { sector: 2, delta_s: 0.082 },
      { sector: 3, delta_s: -0.165 }],
    delta_trace: delta,
    corners: [3, 7, 10, 13].map((n, i) => ({
      corner_number: n, distance: n * 300, entry_speed: 228, min_speed: 98 + i * 5,
      exit_speed: 165, braking_point_m: 125, throttle_delay_s: 0.04,
      time_delta_s: [0.082, -0.07, 0.03, -0.09][i],
    })),
    driver_trace: { session_key: 9159, driver_number: 1, lap_time: 73.672, samples: you },
    reference_trace: { session_key: 9159, driver_number: 4, lap_time: 73.911, samples: ref },
    warnings: [],
  };
}

export function mockTires(): TireAnalysis {
  return {
    session_key: 9159, driver_number: 1, compound: "MEDIUM", tyre_age_laps: 23,
    degradation_s_per_lap: 0.08, remaining_competitive_laps: 7,
    recommended_pit_window: [28, 32], model_confidence: 0.87, model_name: "baseline-linear",
    predictions: [0, 5, 10].map((k) => ({
      laps_ahead: k, predicted_lap_time_s: 74.128 + k * 0.19,
      lower_s: 74.0 + k * 0.19, upper_s: 74.25 + k * 0.19,
    })),
  };
}
