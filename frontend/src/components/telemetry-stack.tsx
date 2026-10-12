"use client";
/**
 * Five stacked subplots sharing one distance axis: Speed, Throttle, Brake, Gear, Delta.
 * One shared x-axis means zoom/pan on any subplot applies to all and double-click resets.
 * Hover and corner selection never touch Plotly: the cursor line and the selected-segment band
 * are plain DOM overlays positioned from the x-axis mapping and moved from a store
 * subscription. (Moving a Plotly shape would be a "calc" edit, which recomputes all 11 traces ×
 * ~2,700 points on every mouse move: ~80 ms per hover, measured.)
 */
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef } from "react";

import type { ComparisonResponse } from "@/lib/api/endpoints";
import { driverStyles, useThemePalette, type DriverStyles, type ThemePalette } from "@/lib/chart-colors";
import { formatDelta, formatDistance, formatGear, formatPercent, formatSpeed } from "@/lib/format";
import { useUiStore } from "@/lib/store";


const Plot = dynamic(
  async () => {
    const [{ default: Plotly }, { default: createPlotlyComponent }] = await Promise.all([
      import("plotly.js-basic-dist"),
      import("react-plotly.js/factory"),
    ]);
    return createPlotlyComponent(Plotly);
  },
  { ssr: false, loading: () => <div className="skeleton h-[640px] w-full" aria-label="Loading charts" /> },
);

// y-axis domains, top to bottom (fractions of the plot height)
const ROWS = {
  speed: [0.6, 1.0],
  throttle: [0.43, 0.57],
  brake: [0.33, 0.4],
  gear: [0.2, 0.3],
  delta: [0.0, 0.17],
} as const;

export function buildFigure(cmp: ComparisonResponse, styles: DriverStyles, palette: ThemePalette) {
  const x = cmp.distance_m;
  const line = (s: DriverStyles["reference"], width = 1.6) => ({ color: s.color, width, dash: s.dash });
  const both = (channel: keyof typeof cmp.channels.reference, yaxis: string, extra: Record<string, unknown> = {}, scale?: (v: unknown) => unknown) =>
    (["reference", "target"] as const).map((side) => {
      const values = cmp.channels[side][channel] as unknown[];
      return {
        type: "scatter", mode: "lines", x, y: scale ? values.map(scale) : values, yaxis,
        name: styles[side].label, legendgroup: side, showlegend: channel === "speed_kph",
        line: line(styles[side], side === "reference" ? 1.7 : 1.5),
        hovertemplate: `%{y}<extra>${styles[side].label}</extra>`,
        ...extra,
      };
    });
  const pos = cmp.delta_s.map((d) => (d > 0 ? d : 0));
  const neg = cmp.delta_s.map((d) => (d < 0 ? d : 0));

  const data = [
    ...both("speed_kph", "y", { hovertemplate: "%{y:.1f} km/h<extra>%{fullData.name}</extra>" }),
    ...both("throttle_pct", "y2", { hovertemplate: "%{y:.0f}%<extra>%{fullData.name}</extra>" }),
    // brake: filled 0/1 steps, never a smooth line
    ...both("brake", "y3", { line: undefined, fill: "tozeroy", hovertemplate: "%{y}<extra>%{fullData.name} brake</extra>" },
      (v) => (v ? 1 : 0)).map((t, i) => ({
      ...t, line: { shape: "hv", width: 1, color: i === 0 ? styles.reference.color : styles.target.color, dash: i === 0 ? "solid" : styles.target.dash },
      fillcolor: `${i === 0 ? styles.reference.color : styles.target.color}${i === 0 ? "55" : "33"}`,
    })),
    ...both("gear", "y4", { hovertemplate: "gear %{y}<extra>%{fullData.name}</extra>" }).map((t) => ({ ...t, line: { ...t.line, shape: "hv" } })),
    { type: "scatter", mode: "lines", x, y: pos, yaxis: "y5", fill: "tozeroy", name: "Target slower",
      line: { width: 0, color: palette.red }, fillcolor: `${palette.red}66`, hoverinfo: "skip", showlegend: false },
    { type: "scatter", mode: "lines", x, y: neg, yaxis: "y5", fill: "tozeroy", name: "Target faster",
      line: { width: 0, color: palette.green }, fillcolor: `${palette.green}66`, hoverinfo: "skip", showlegend: false },
    { type: "scatter", mode: "lines", x, y: cmp.delta_s, yaxis: "y5", name: "Δ time",
      line: { width: 1.4, color: palette.ink }, showlegend: false,
      hovertemplate: "%{y:+.3f} s<extra>Δ (target − reference)</extra>" },
  ];

  const axis = (domain: readonly [number, number], title: string, extra: Record<string, unknown> = {}) => ({
    domain, title: { text: title, font: { size: 10, color: palette.muted } }, gridcolor: palette.line, zeroline: false,
    tickfont: { size: 10, color: palette.muted }, fixedrange: true, ...extra,
  });

  const corners = cmp.findings.corners.filter((f) => f.segment?.apex_m != null);
  // labels closer than ~150 m alternate between two rows so chicanes stay readable
  const rows: number[] = [];
  corners.forEach((f, i) => {
    const crowded = i > 0 && f.segment!.apex_m! - corners[i - 1]!.segment!.apex_m! < 150;
    rows.push(crowded ? 1 - rows[i - 1]! : 0);
  });
  const layout = {
    autosize: true, height: 640, margin: { l: 56, r: 14, t: 46, b: 36 },
    paper_bgcolor: palette.card, plot_bgcolor: palette.card, font: { color: palette.ink, size: 11 },
    hovermode: "x unified", hoversubplots: "axis", hoverlabel: { bgcolor: palette.card, bordercolor: palette.line, font: { color: palette.ink } },
    // the card heading names both drivers in their line colours, so no Plotly legend
    dragmode: "zoom", showlegend: false,
    xaxis: { title: { text: "Distance (m)", font: { size: 10, color: palette.muted } }, gridcolor: palette.line, zeroline: false,
      tickfont: { size: 10, color: palette.muted }, range: [0, x[x.length - 1]], showspikes: false, anchor: "y5" },
    yaxis: axis(ROWS.speed, "Speed km/h"),
    yaxis2: axis(ROWS.throttle, "Throttle %", { range: [-5, 105] }),
    yaxis3: axis(ROWS.brake, "Brake", { range: [0, 1.15], tickvals: [0, 1], ticktext: ["off", "on"] }),
    yaxis4: axis(ROWS.gear, "Gear", { range: [0.5, 8.5], dtick: 2 }),
    yaxis5: axis(ROWS.delta, "Δ s", { zeroline: true, zerolinecolor: palette.muted, zerolinewidth: 1 }),
    // corner numbers along the top; clicking one selects the corner
    annotations: corners.map((f, i) => ({
      x: f.segment!.apex_m, xref: "x", y: 1.0, yref: "paper", yanchor: "bottom", showarrow: false,
      yshift: rows[i] ? 12 : 0,
      text: f.corner_number != null ? `T${f.corner_number}` : "·", captureevents: true,
      font: { size: 10, color: palette.muted }, hovertext: `${f.label}: ${formatDelta(f.time_loss_s)}`,
    })),
  };
  return { data, layout, cornerIds: corners.map((f) => f.id) };
}

export function TelemetryStack({ comparison }: { comparison: ComparisonResponse }) {
  const palette = useThemePalette();
  const styles = useMemo(() => driverStyles(comparison, palette), [comparison, palette]);
  const figure = useMemo(() => buildFigure(comparison, styles, palette), [comparison, styles, palette]);
  const graphDiv = useRef<HTMLElement | null>(null);
  const cursorEl = useRef<HTMLDivElement | null>(null);
  const bandEl = useRef<HTMLDivElement | null>(null);
  const frame = useRef<number | null>(null);
  const segments = useMemo(() => new Map(comparison.findings.corners.map((f) => [f.id, f.segment])), [comparison]);

  // Position the overlays from the current x-axis mapping (also after zoom / resize).
  const placeOverlays = useCallback(() => {
    const gd = graphDiv.current as PlotlyGraphDiv | null;
    const geo = gd ? plotGeometry(gd) : null;
    const { hoverDistance, selectedCornerId } = useUiStore.getState();
    const cursor = cursorEl.current, band = bandEl.current;
    if (!geo || !cursor || !band) return;
    for (const el of [cursor, band]) { el.style.top = `${geo.top}px`; el.style.height = `${geo.height}px`; }
    const inView = (d: number) => d >= geo.range[0] && d <= geo.range[1];
    if (hoverDistance !== null && inView(hoverDistance)) {
      cursor.style.transform = `translateX(${geo.toPx(hoverDistance)}px)`;
      cursor.style.visibility = "visible";
    } else cursor.style.visibility = "hidden";
    const seg = selectedCornerId ? segments.get(selectedCornerId) : null;
    if (seg && seg.end_m > geo.range[0] && seg.start_m < geo.range[1]) {
      const a = geo.toPx(Math.max(seg.start_m, geo.range[0])), b = geo.toPx(Math.min(seg.end_m, geo.range[1]));
      band.style.transform = `translateX(${a}px)`;
      band.style.width = `${Math.max(1, b - a)}px`;
      band.style.visibility = "visible";
    } else band.style.visibility = "hidden";
  }, [segments]);
  const setHover = useUiStore((s) => s.setHover);
  const selectCorner = useUiStore((s) => s.selectCorner);

  // store → overlays, imperatively, at most once per animation frame
  useEffect(() => {
    const unsubscribe = useUiStore.subscribe((s, prev) => {
      if (s.hoverDistance === prev.hoverDistance && s.selectedCornerId === prev.selectedCornerId) return;
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      frame.current = requestAnimationFrame(placeOverlays);
    });
    return () => { unsubscribe(); if (frame.current !== null) cancelAnimationFrame(frame.current); };
  }, [placeOverlays]);

  return (
    <section id="telemetry" className="card min-w-0" aria-labelledby="telemetry-title">
      <div className="mb-2 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="kick">Telemetry comparison</div>
          <h2 id="telemetry-title" className="text-[15px] font-bold">
            <span style={{ color: styles.target.text }}>{styles.target.label}</span>
            <span className="text-muted"> vs </span>
            <span style={{ color: styles.reference.text }}>{styles.reference.label}</span>
          </h2>
        </div>
        <p className="text-xs text-muted">Drag to zoom any row (all rows follow) · double-click to reset · click a T-number to inspect it</p>
      </div>
      <HoverReadout comparison={comparison} styles={styles} />
      <div data-testid="telemetry-plot" role="img" className="relative"
        aria-label={`Speed, throttle, brake, gear and time-delta traces for ${styles.target.label} versus ${styles.reference.label} by distance.`}>
        <Plot
          data={figure.data}
          layout={figure.layout}
          config={{ displaylogo: false, responsive: true, scrollZoom: false, showTips: false, modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"] }}
          style={{ width: "100%" }}
          useResizeHandler
          onInitialized={(_, gd) => { graphDiv.current = gd; placeOverlays(); }}
          onUpdate={(_, gd) => { graphDiv.current = gd; placeOverlays(); }}
          onRelayout={placeOverlays}
          onHover={(e: { xvals?: number[]; points?: { x: number }[] }) => {
            const d = e.xvals?.[0] ?? e.points?.[0]?.x;
            if (typeof d === "number") setHover(d);
          }}
          onUnhover={() => setHover(null)}
          onClickAnnotation={(e: { index: number }) => {
            const id = figure.cornerIds[e.index];
            if (id) selectCorner(id);
          }}
        />
        <div ref={bandEl} aria-hidden data-testid="selection-band"
          className="pointer-events-none absolute left-0 bg-[var(--accent)] opacity-10" style={{ visibility: "hidden" }} />
        <div ref={cursorEl} aria-hidden data-testid="chart-cursor"
          className="pointer-events-none absolute left-0 w-0 border-l border-dotted border-[var(--ink)]" style={{ visibility: "hidden" }} />
      </div>
    </section>
  );
}

/** The original dashboard's per-row live values, now driven by the shared hover distance. */
function HoverReadout({ comparison, styles }: { comparison: ComparisonResponse; styles: DriverStyles }) {
  const hover = useUiStore((s) => s.hoverDistance);
  const x = comparison.distance_m;
  const i = hover === null ? -1 : nearestIndex(x, hover);
  const ref = comparison.channels.reference, tgt = comparison.channels.target;
  const at = <T,>(arr: T[]) => (i >= 0 ? arr[i] : undefined);
  const cell = (label: string, r: string, t: string) => (
    <div className="min-w-[96px]">
      <div className="kick">{label}</div>
      <div className="num-tab text-sm font-bold" style={{ color: styles.reference.text }}>{r}</div>
      <div className="num-tab text-xs font-semibold" style={{ color: styles.target.text }}>{t}</div>
    </div>
  );
  return (
    <div className="mb-1 flex flex-wrap gap-x-5 gap-y-2 rounded-xl border border-line bg-card-2 px-3 py-2" aria-live="off">
      <div className="min-w-[90px]">
        <div className="kick">Distance</div>
        <div className="num-tab text-sm font-bold">{i >= 0 ? formatDistance(x[i]) : "hover a chart"}</div>
      </div>
      {cell("Speed", formatSpeed(at(ref.speed_kph)), formatSpeed(at(tgt.speed_kph)))}
      {cell("Throttle", formatPercent(at(ref.throttle_pct)), formatPercent(at(tgt.throttle_pct)))}
      {cell("Brake", i >= 0 ? (at(ref.brake) ? "on" : "off") : "—", i >= 0 ? (at(tgt.brake) ? "on" : "off") : "—")}
      {cell("Gear", formatGear(at(ref.gear)), formatGear(at(tgt.gear)))}
      <div className="min-w-[120px]">
        <div className="kick">Δ time</div>
        <div className={`num-tab text-sm font-bold ${(at(comparison.delta_s) ?? 0) > 0 ? "pos" : "neg"}`}>
          {i >= 0 ? formatDelta(at(comparison.delta_s)) : "—"}
        </div>
      </div>
    </div>
  );
}

export function nearestIndex(sorted: number[], value: number): number {
  let lo = 0, hi = sorted.length - 1;
  if (hi < 0) return -1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid]! <= value) lo = mid; else hi = mid;
  }
  return Math.abs(sorted[hi]! - value) < Math.abs(sorted[lo]! - value) ? hi : lo;
}

/** The few Plotly internals the overlays need (stable across plotly.js 2–4). */
interface PlotlyGraphDiv extends HTMLElement {
  _fullLayout?: {
    _size: { t: number; h: number };
    xaxis: { _offset: number; range: [number, number]; l2p: (v: number) => number };
  };
}

export function plotGeometry(gd: PlotlyGraphDiv) {
  const fl = gd._fullLayout;
  if (!fl?.xaxis || !fl._size) return null;
  const xa = fl.xaxis;
  return {
    top: fl._size.t,
    height: fl._size.h,
    range: [Math.min(...xa.range), Math.max(...xa.range)] as const,
    toPx: (d: number) => xa._offset + xa.l2p(d),
  };
}
