"use client";

import type { DriverInfo, LapRecord } from "@/lib/api/endpoints";
import { MISSING, formatLapTime } from "@/lib/format";
import type { LapRef } from "@/lib/url-state";

export interface LapPickerProps {
  role: "reference" | "target";
  value: LapRef | null;
  drivers: DriverInfo[];
  laps: LapRecord[];
  loading: boolean;
  onChange: (value: LapRef | null) => void;
}

const REASONS: Record<string, string> = {
  no_lap_time: "no lap time",
  not_accurate: "timing not accurate",
  deleted: "deleted",
  no_telemetry: "no telemetry",
  too_few_samples: "too few samples",
  distance_out_of_range: "distance out of range",
};

export function invalidLabel(reason: string | null | undefined): string {
  return reason ? REASONS[reason] ?? reason.replace(/_/g, " ") : "not comparable";
}

export function fastestValid(laps: LapRecord[], driver: string): LapRecord | null {
  let best: LapRecord | null = null;
  for (const lap of laps) {
    if (lap.driver !== driver || !lap.is_valid_for_comparison || lap.lap_time_s == null) continue;
    if (!best || lap.lap_time_s < (best.lap_time_s ?? Infinity)) best = lap;
  }
  return best;
}

/** Driver select + lap select ("Fastest valid" default). Invalid laps are listed but disabled. */
export function LapPicker({ role, value, drivers, laps, loading, onChange }: LapPickerProps) {
  const label = role === "reference" ? "Reference" : "Target";
  const driverLaps = value ? laps.filter((l) => l.driver === value.driver).sort((a, b) => a.lap_number - b.lap_number) : [];
  const fastest = value ? fastestValid(laps, value.driver) : null;
  const chosen = value?.lap === "fastest" ? fastest : driverLaps.find((l) => l.lap_number === value?.lap) ?? null;
  const team = drivers.find((d) => d.code === value?.driver);
  const id = `pick-${role}`;

  return (
    <div className={`strip-cell ${role === "target" ? "cmp" : ""}`} data-testid={id}>
      <div className="flex flex-col gap-2 sm:flex-row sm:gap-4">
        <div className="min-w-0 flex-1">
          <label className="field-label" htmlFor={`${id}-driver`}>
            <span aria-hidden className="mr-1.5 inline-block h-2 w-2 rounded-sm align-middle"
              style={{ background: team?.team_color ?? "var(--muted)" }} />
            {label} driver
          </label>
          <select id={`${id}-driver`} className="field-select" disabled={loading || drivers.length === 0}
            value={value?.driver ?? ""}
            onChange={(e) => onChange(e.target.value ? { driver: e.target.value, lap: "fastest" } : null)}>
            <option value="">{loading ? "Loading drivers…" : "Choose a driver"}</option>
            {drivers.map((d) => (
              <option key={d.code} value={d.code}>{d.code} · {d.full_name ?? d.number}{d.team ? ` · ${d.team}` : ""}</option>
            ))}
          </select>
        </div>
        <div className="min-w-0 flex-1">
          <label className="field-label" htmlFor={`${id}-lap`}>{label} lap</label>
          <select id={`${id}-lap`} className="field-select" disabled={!value}
            value={value ? String(value.lap) : ""}
            onChange={(e) => value && onChange({ driver: value.driver, lap: e.target.value === "fastest" ? "fastest" : Number(e.target.value) })}>
            {!value && <option value="">Pick a driver first</option>}
            {value && <option value="fastest">Fastest valid{fastest ? ` (L${fastest.lap_number} · ${formatLapTime(fastest.lap_time_s)})` : " (none)"}</option>}
            {driverLaps.map((lap) => (
              <option key={lap.lap_number} value={lap.lap_number} disabled={!lap.is_valid_for_comparison}>
                L{lap.lap_number} · {formatLapTime(lap.lap_time_s)}{lap.compound ? ` · ${lap.compound}` : ""}
                {lap.is_valid_for_comparison ? "" : ` · ⚠ ${invalidLabel(lap.invalid_reason)}`}
              </option>
            ))}
          </select>
        </div>
      </div>
      {value && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-xs" aria-live="polite">
          {chosen ? (
            <>
              <span className="chip">L{chosen.lap_number} · {formatLapTime(chosen.lap_time_s)}</span>
              <span className="chip">{chosen.compound ?? MISSING}{chosen.tyre_age != null ? ` · ${chosen.tyre_age} laps old` : ""}</span>
              {!chosen.is_valid_for_comparison && <span className="chip text-red-ink">⚠ invalid: {invalidLabel(chosen.invalid_reason)}</span>}
            </>
          ) : (
            <span className="chip text-red-ink">⚠ {value.lap === "fastest" ? `${value.driver} has no valid lap` : `lap ${value.lap} not found`}</span>
          )}
        </div>
      )}
    </div>
  );
}
