"use client";
/**
 * Lap time vs lap for the stint "as of" lap N: observed clean laps (points), excluded laps
 * (hollow, optional), the model's fit, the forecast with its 80% band, the competitive threshold,
 * the pit window (shaded, central lap marked) and the current-lap marker.
 *
 * Everything plotted comes from ONE /tires/predict response for lap N, so no lap after N can
 * appear except as a forecast. The figure is built by a pure function (unit-tested).
 */
import dynamic from "next/dynamic";
import { useMemo } from "react";

import { useThemePalette, type ThemePalette } from "@/lib/chart-colors";
import { windowShading, type TirePrediction } from "@/lib/tires";

const Plot = dynamic(
  async () => {
    const [{ default: Plotly }, { default: createPlotlyComponent }] = await Promise.all([
      import("plotly.js-basic-dist"),
      import("react-plotly.js/factory"),
    ]);
    return createPlotlyComponent(Plotly);
  },
  { ssr: false, loading: () => <div className="skeleton h-[340px] w-full" aria-label="Loading tyre chart" /> },
);

type Trace = Record<string, unknown> & { name: string; x: number[]; y: (number | null)[] };

export interface TireFigure {
  data: Trace[];
  layout: Record<string, unknown>;
  yRange: [number, number];
  offScaleExcluded: number;
}

export function buildTireFigure(p: TirePrediction, palette: ThemePalette, showExcluded: boolean): TireFigure {
  const hist = p.history ?? [];
  const clean = hist.filter((h) => h.clean && h.lap_time_s != null);
  const excluded = hist.filter((h) => !h.clean && h.lap_time_s != null);
  const pred = p.predicted_laps ?? [];
  const thr = p.threshold_curve ?? [];
  const fitted = clean.filter((h) => h.fitted_s != null);

  // y-range from what matters (clean laps, band, threshold); in/out laps would flatten everything
  const ys = [...clean.map((h) => h.lap_time_s!), ...pred.flatMap((q) => [q.lo_s, q.hi_s]), ...thr.map((t) => t.threshold_s)];
  const lo = ys.length ? Math.min(...ys) : 0;
  const hi = ys.length ? Math.max(...ys) : 1;
  const pad = Math.max(0.3, (hi - lo) * 0.08);
  const yRange: [number, number] = [lo - pad, hi + pad];
  const offScaleExcluded = excluded.filter((h) => h.lap_time_s! < yRange[0] || h.lap_time_s! > yRange[1]).length;

  const data: Trace[] = [];
  if (pred.length) {
    data.push({ type: "scatter", mode: "lines", name: "80% band (low)", x: pred.map((q) => q.lap), y: pred.map((q) => q.lo_s),
      line: { width: 0 }, hoverinfo: "skip", showlegend: false });
    data.push({ type: "scatter", mode: "lines", name: "80% prediction band", x: pred.map((q) => q.lap), y: pred.map((q) => q.hi_s),
      fill: "tonexty", fillcolor: "rgba(59,125,216,0.18)", line: { width: 0 }, hoverinfo: "skip" });
  }
  if (thr.length) {
    data.push({ type: "scatter", mode: "lines", name: "Competitive threshold (best + X)", x: thr.map((t) => t.lap),
      y: thr.map((t) => t.threshold_s), line: { color: palette.red, width: 1.4, dash: "dash" },
      hovertemplate: "lap %{x}: %{y:.3f} s<extra>threshold</extra>" });
  }
  if (fitted.length) {
    data.push({ type: "scatter", mode: "lines", name: "Model fit (laps ≤ as-of)", x: fitted.map((h) => h.lap),
      y: fitted.map((h) => h.fitted_s!), line: { color: palette.muted, width: 1.4, dash: "dot" }, hoverinfo: "skip" });
  }
  if (pred.length) {
    data.push({ type: "scatter", mode: "lines", name: "Forecast", x: pred.map((q) => q.lap), y: pred.map((q) => q.pred_s),
      line: { color: palette.accent, width: 2 }, hovertemplate: "lap %{x}: %{y:.3f} s<extra>forecast</extra>" });
  }
  if (showExcluded && excluded.length) {
    data.push({ type: "scatter", mode: "markers", name: "Excluded laps", x: excluded.map((h) => h.lap),
      y: excluded.map((h) => h.lap_time_s), text: excluded.map((h) => h.excluded_reason ?? "excluded"),
      marker: { size: 8, color: "rgba(0,0,0,0)", line: { color: palette.neutral, width: 1.5 } },
      hovertemplate: "lap %{x}: %{y:.3f} s<extra>excluded: %{text}</extra>" });
  }
  data.push({ type: "scatter", mode: "markers", name: "Observed clean laps", x: clean.map((h) => h.lap),
    y: clean.map((h) => h.lap_time_s), marker: { size: 7, color: palette.ink },
    hovertemplate: "lap %{x}: %{y:.3f} s<extra>observed</extra>" });

  const shapes: Record<string, unknown>[] = [];
  const end = pred.length ? pred[pred.length - 1]!.lap : null;
  const shade = windowShading(p.pit_window, end);
  if (shade) {
    shapes.push({ type: "rect", xref: "x", yref: "paper", x0: shade.x0, x1: shade.x1, y0: 0, y1: 1,
      fillcolor: "rgba(226,119,90,0.16)", line: { width: 0 }, layer: "below" });
    if (shade.central != null) {
      shapes.push({ type: "line", xref: "x", yref: "paper", x0: shade.central, x1: shade.central, y0: 0, y1: 1,
        line: { color: palette.accent, width: 2 } });
    }
  }
  shapes.push({ type: "line", xref: "x", yref: "paper", x0: p.lap, x1: p.lap, y0: 0, y1: 1,
    line: { color: palette.ink, width: 1, dash: "dashdot" } });

  const firstLap = hist.length ? hist[0]!.lap : p.lap;
  const lastLap = Math.max(p.lap, end ?? p.lap);
  return {
    data,
    yRange,
    offScaleExcluded,
    layout: {
      autosize: true, height: 460, margin: { l: 56, r: 12, t: 8, b: 40 },
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: palette.ink, size: 11 },
      xaxis: { title: { text: "Lap" }, range: [firstLap - 0.5, lastLap + 0.5], gridcolor: palette.line, zeroline: false },
      yaxis: { title: { text: "Lap time (s)" }, range: yRange, gridcolor: palette.line, zeroline: false },
      legend: { orientation: "h", y: -0.22, font: { size: 10 } },
      shapes,
      annotations: [{ x: p.lap, y: 1, xref: "x", yref: "paper", text: `as of lap ${p.lap}`, showarrow: false,
        xanchor: "right", yanchor: "top", xshift: -4, font: { size: 10, color: palette.muted } }],
    },
  };
}

export function TireChart({ prediction, showExcluded, describedBy }: {
  prediction: TirePrediction; showExcluded: boolean; describedBy: string;
}) {
  const palette = useThemePalette();
  const fig = useMemo(() => buildTireFigure(prediction, palette, showExcluded), [prediction, palette, showExcluded]);
  return (
    <div data-testid="tire-chart" className="chart-reveal" role="img" aria-label={`Tyre forecast chart as of lap ${prediction.lap}`}
      aria-describedby={describedBy}>
      <Plot data={fig.data} layout={fig.layout} config={{ displayModeBar: false, responsive: true }}
        style={{ width: "100%", height: 460 }} useResizeHandler />
      {fig.offScaleExcluded > 0 && showExcluded && (
        <p className="mt-1 text-[11px] text-muted">{fig.offScaleExcluded} excluded lap{fig.offScaleExcluded === 1 ? "" : "s"} (in/out laps) off the scale.</p>
      )}
    </div>
  );
}
