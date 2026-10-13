"use client";
/**
 * Qualifying (MVP): lap time vs tyre age for one driver across the session, fresh vs used sets.
 * Observed laps only, never a prediction (the subtitle says so).
 */
import dynamic from "next/dynamic";
import { useId, useMemo } from "react";

import type { LapRecord } from "@/lib/api/endpoints";
import { useThemePalette } from "@/lib/chart-colors";
import { formatLapTime } from "@/lib/format";
import { clampCompound, COMPOUNDS } from "@/lib/tires";

const Plot = dynamic(
  async () => {
    const [{ default: Plotly }, { default: createPlotlyComponent }] = await Promise.all([
      import("plotly.js-basic-dist"),
      import("react-plotly.js/factory"),
    ]);
    return createPlotlyComponent(Plotly);
  },
  { ssr: false, loading: () => <div className="skeleton h-[280px] w-full" aria-label="Loading qualifying tyre chart" /> },
);

export function qualifyingPoints(laps: LapRecord[], driver: string) {
  return laps
    .filter((l) => l.driver === driver && l.lap_time_s != null && l.tyre_age != null && l.is_accurate && !l.deleted)
    .map((l) => ({ lap: l.lap_number, time: l.lap_time_s!, age: l.tyre_age!, fresh: l.fresh_tyre, compound: clampCompound(l.compound) }));
}

export function QualifyingTireChart({ laps, driver }: { laps: LapRecord[]; driver: string }) {
  const palette = useThemePalette();
  const id = useId();
  const points = useMemo(() => qualifyingPoints(laps, driver), [laps, driver]);
  const groups = [
    { name: "Fresh set", pts: points.filter((p) => p.fresh === true), symbol: "circle" },
    { name: "Used set", pts: points.filter((p) => p.fresh !== true), symbol: "circle-open" },
  ];
  const summary = points.length
    ? `${driver}: ${points.length} timed laps. ` + points.map((p) =>
      `lap ${p.lap} ${formatLapTime(p.time)} on ${COMPOUNDS[p.compound].label.toLowerCase()} aged ${p.age} (${p.fresh ? "fresh" : "used"})`).join("; ") + "."
    : `${driver}: no accurate timed laps.`;
  return (
    <section data-testid="qualifying-tire-chart" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="text-sm font-bold">Lap time vs tyre age · {driver}</h3>
      <p className="text-[11px] text-muted">Observed laps — not a prediction</p>
      <div role="img" aria-label={`Qualifying lap time vs tyre age for ${driver}`} aria-describedby={`${id}-summary`}>
        <Plot
          data={groups.filter((g) => g.pts.length).map((g) => ({
            type: "scatter", mode: "markers", name: g.name, x: g.pts.map((p) => p.age), y: g.pts.map((p) => p.time),
            text: g.pts.map((p) => `lap ${p.lap} · ${COMPOUNDS[p.compound].label}`),
            marker: { size: 10, symbol: g.symbol, color: g.pts.map((p) => COMPOUNDS[p.compound].color),
              line: { color: palette.ink, width: 1.2 } },
            hovertemplate: "%{text}<br>tyre age %{x}: %{y:.3f} s<extra>%{fullData.name}</extra>",
          }))}
          layout={{
            autosize: true, height: 280, margin: { l: 56, r: 12, t: 8, b: 40 },
            paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: palette.ink, size: 11 },
            xaxis: { title: { text: "Tyre age (laps)" }, gridcolor: palette.line, dtick: 1 },
            yaxis: { title: { text: "Lap time (s)" }, gridcolor: palette.line },
            legend: { orientation: "h", y: -0.25 },
          }}
          config={{ displayModeBar: false, responsive: true }} style={{ width: "100%", height: 280 }} useResizeHandler />
      </div>
      <p id={`${id}-summary`} className="sr-only">{summary}</p>
    </section>
  );
}
