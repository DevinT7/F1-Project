"use client";
/**
 * One horizontal bar per stint, sized by laps, in the standard compound colours AND labelled with
 * the compound letter and name (colour is never the only cue). Clicking a stint selects it and
 * sets the "as of" lap.
 */
import { clampCompound, COMPOUNDS, type StintSummary } from "@/lib/tires";

export interface StintTimelineProps {
  driver: string;
  stints: StintSummary[];
  raceLaps: number;
  selectedStint: number | null;
  onSelect: (stint: StintSummary) => void;
}

function stintLabel(s: StintSummary): string {
  const c = COMPOUNDS[clampCompound(s.compound)];
  const age = s.tyre_age_at_start == null ? "unknown age" : `age ${s.tyre_age_at_start} at start`;
  const set = s.fresh_tyre === true ? "new set" : s.fresh_tyre === false ? "used set" : "set unknown";
  return `Stint ${s.stint}: ${c.label}, laps ${s.first_lap}–${s.last_lap} (${s.total_laps} laps, ${age}, ${set}), `
    + `${s.clean_laps} clean laps`;
}

export function StintTimeline({ driver, stints, raceLaps, selectedStint, onSelect }: StintTimelineProps) {
  const total = Math.max(raceLaps, ...stints.map((s) => s.last_lap), 1);
  return (
    <div data-testid="stint-timeline" aria-label={`${driver} stints`} role="group">
      <div className="relative flex h-11 w-full overflow-hidden rounded-lg border border-line bg-card-2">
        {stints.map((s) => {
          const c = COMPOUNDS[clampCompound(s.compound)];
          const left = ((s.first_lap - 1) / total) * 100;
          const width = (s.total_laps / total) * 100;
          const selected = s.stint === selectedStint;
          const dark = clampCompound(s.compound) === "WET" || clampCompound(s.compound) === "SOFT";
          return (
            <button key={s.stint} type="button" data-testid={`stint-${s.stint}`}
              aria-pressed={selected} aria-label={stintLabel(s)} title={stintLabel(s)}
              onClick={() => onSelect(s)}
              className={`absolute top-0 flex h-full items-center gap-1.5 overflow-hidden border-r-2 border-card px-2 text-left text-[11px] font-bold ${selected ? "ring-2 ring-inset ring-ink" : "opacity-85 hover:opacity-100"}`}
              style={{ left: `${left}%`, width: `${width}%`, background: c.color, color: dark ? "#fff" : "#1b1a19" }}>
              <span aria-hidden className="flex h-5 w-5 flex-none items-center justify-center rounded-full border-2 bg-white/85 text-[10px] text-[#1b1a19]"
                style={{ borderColor: c.color === "#f0f0ec" ? "#8f8a86" : c.color }}>{c.letter}</span>
              <span aria-hidden className="truncate">{c.label} · {s.total_laps}L{s.tyre_age_at_start != null && s.tyre_age_at_start > 1 ? ` · used ${s.tyre_age_at_start}` : ""}</span>
            </button>
          );
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-muted" aria-hidden>
        <span>Lap 1</span><span>Lap {total}</span>
      </div>
    </div>
  );
}
