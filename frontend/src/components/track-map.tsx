"use client";
/**
 * SVG circuit from the reference lap's x/y, split into the Phase 3 corner segments. Two views:
 *
 *  - Time gained / lost: segments coloured by time loss (green = target gained, red = target lost,
 *    grey = |loss| < 0.02 s). Colour is never the only cue: each corner carries ▲/▼/● and its
 *    magnitude in a tooltip, and every corner is a keyboard-focusable button.
 *  - Lap replay: each driver is a dot in their team colour, starting at the start/finish line and
 *    moving at their real pace (elapsed lap time vs distance), so the gap opening and closing is
 *    visible where it happens. Clicking a corner moves the faster driver to it and puts the slower
 *    one where they were at that same instant.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ComparisonResponse, CornersResponse, Finding } from "@/lib/api/endpoints";
import { driverStyles, useThemePalette, type ThemePalette } from "@/lib/chart-colors";
import { lossStyle, NOISE_FLOOR_S, sameTeamColor } from "@/lib/colors";
import { formatDelta } from "@/lib/format";
import {
  JUMP_MS, SPEEDS, buildReplay, distanceAt, easeInOut, gapAt, jumpToCorner, trendAt, type CornerJump, type Replay,
  type Side,
} from "@/lib/replay";
import { useUiStore } from "@/lib/store";
import { buildTrack, nearestDistance, pointAt, segmentPath, type TrackGeometry } from "@/lib/track";

const SIZE = { width: 420, height: 320, padding: 22 };
// leader-line labels sit outside the track: more room around it
const LEADER_SIZE = { width: 420, height: 320, padding: 34 };
type Mode = "delta" | "replay";
/** "leaders": a short line out of each corner to its turn label · "none": the bare layout */
export type CornerLabelMode = "leaders" | "none";

export function cornerShortLabel(f: Finding): string {
  return f.corner_number != null ? `T${f.corner_number}` : "•";
}

interface Seg { f: Finding; d: string; style: ReturnType<typeof lossStyle> }

export function TrackMap({ comparison, corners, maxHeight = 340, labels = "leaders" }: {
  comparison: ComparisonResponse; corners: CornersResponse | undefined; maxHeight?: number; labels?: CornerLabelMode;
}) {
  const palette = useThemePalette();
  const [mode, setMode] = useState<Mode>("delta");
  const ref = comparison.channels.reference;
  const track = useMemo(
    () => buildTrack(comparison.distance_m, ref.x, ref.y, corners?.circuit_rotation_deg ?? 0,
      labels === "leaders" ? LEADER_SIZE : SIZE),
    [comparison, ref.x, ref.y, corners?.circuit_rotation_deg, labels],
  );
  const findings = comparison.findings.corners;
  const segments = useMemo<Seg[]>(
    () => (track ? findings.map((f) => ({ f, d: segmentPath(track, f.segment!.start_m, f.segment!.end_m), style: lossStyle(f.time_loss_s) })) : []),
    [track, findings],
  );

  if (!track) {
    return <p className="text-sm text-muted">No position data for this lap, so the map can&apos;t be drawn.</p>;
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-1.5" role="group" aria-label="Track map view">
        {([["delta", "Time gained / lost"], ["replay", "Lap replay"]] as const).map(([m, label]) => (
          <button key={m} type="button" onClick={() => setMode(m)} aria-pressed={mode === m} data-testid={`map-mode-${m}`}
            className={`chip cursor-pointer ${mode === m ? "ring-2 ring-[var(--accent)] text-ink" : ""}`}>{label}</button>
        ))}
      </div>
      {mode === "delta"
        ? <DeltaMap track={track} segments={segments} palette={palette} maxHeight={maxHeight} labels={labels} />
        : <ReplayMap track={track} segments={segments} palette={palette} comparison={comparison} maxHeight={maxHeight} labels={labels} />}
    </div>
  );
}

// ---------------------------------------------------------------- time gained / lost
function DeltaMap({ track, segments, palette, maxHeight, labels }: {
  track: TrackGeometry; segments: Seg[]; palette: ThemePalette; maxHeight: number; labels: CornerLabelMode;
}) {
  const selected = useUiStore((s) => s.selectedCornerId);
  const selectCorner = useUiStore((s) => s.selectCorner);
  const setHover = useUiStore((s) => s.setHover);

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const local = svgPoint(e);
    if (local) setHover(nearestDistance(track, local.x, local.y));
  }

  return (
    <div>
      <svg viewBox={`0 0 ${SIZE.width} ${SIZE.height}`} className="w-full" style={{ maxHeight }}
        role="img" aria-labelledby="track-title track-desc" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <title id="track-title">Track map coloured by where the target gained or lost time</title>
        <desc id="track-desc">Each corner segment is green when the target gained time, red when it lost time, and grey within ±{NOISE_FLOOR_S} s.</desc>
        <BaseLine track={track} palette={palette} />
        {segments.map(({ f, d, style }) => (
          <path key={f.id} d={d} fill="none" stroke={style.color}
            strokeWidth={f.id === selected ? 5.5 : 3.5} strokeLinecap="round" strokeLinejoin="round"
            opacity={selected && f.id !== selected ? 0.55 : 1} onClick={() => selectCorner(f.id)} className="track-seg cursor-pointer">
            <title>{`${f.label}: ${style.glyph} ${style.label}`}</title>
          </path>
        ))}
        <StartFinish track={track} palette={palette} />
        {labels === "leaders" && <CornerLabels track={track} segments={segments} palette={palette} onPick={(f) => selectCorner(f.id)} />}
      </svg>
      <ul className="mt-2 flex flex-wrap justify-center gap-4 text-xs text-muted" aria-label="Map legend">
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(-0.2).color }} />▼ Target gaining</li>
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(0.2).color }} />▲ Target losing</li>
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(0).color }} />● Level (&lt; {NOISE_FLOOR_S} s)</li>
      </ul>
      {labels === "leaders" && <CornerChips segments={segments} onPick={(f) => selectCorner(f.id)} />}
    </div>
  );
}

// ---------------------------------------------------------------- lap replay
function ReplayMap({ track, segments, palette, comparison, maxHeight, labels }: {
  track: TrackGeometry; segments: Seg[]; palette: ThemePalette; comparison: ComparisonResponse; maxHeight: number;
  labels: CornerLabelMode;
}) {
  const selectCorner = useUiStore((s) => s.selectCorner);
  const replay = useMemo(() => buildReplay(comparison.distance_m, comparison.channels.reference.time_s,
    comparison.channels.target.time_s), [comparison]);
  const styles = useMemo(() => driverStyles(comparison, palette), [comparison, palette]);
  const sameTeam = sameTeamColor(comparison.meta.reference.team_color, comparison.meta.target.team_color);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(4);
  const [jump, setJump] = useState<{ f: Finding; j: CornerJump } | null>(null);
  const tRef = useRef(0);
  const raf = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
    raf.current = null;
  }, []);
  const set = useCallback((value: number) => { tRef.current = value; setT(value); }, []);

  // real-time playback (lap seconds × speed)
  useEffect(() => {
    if (!playing || !replay) return;
    let last = performance.now();
    const step = () => {
      // one clock (performance.now), and a frame never advances more than 100 ms of wall time:
      // a backgrounded tab resumes where it was instead of jumping to the end
      const now = performance.now();
      const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
      const next = Math.min(replay.duration, tRef.current + dt * speed);
      last = now;
      set(next);
      if (next >= replay.duration) { setPlaying(false); raf.current = null; return; }
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return stop;
  }, [playing, speed, replay, set, stop]);

  useEffect(() => stop, [stop]);

  if (!replay) return <p className="text-sm text-muted">No timing data for this lap, so the replay can&apos;t run.</p>;

  function animateTo(target: number) {
    stop();
    setPlaying(false);
    const from = tRef.current, start = performance.now();
    const step = () => {
      const x = Math.min(1, (performance.now() - start) / JUMP_MS);
      set(from + (target - from) * easeInOut(x));
      raf.current = x < 1 ? requestAnimationFrame(step) : null;
    };
    raf.current = requestAnimationFrame(step);
  }

  function pickCorner(f: Finding) {
    const j = jumpToCorner(replay!, f.segment!.apex_m ?? f.segment!.start_m);
    selectCorner(f.id);
    setJump({ f, j });
    animateTo(j.t);
  }

  function togglePlay() {
    setJump(null);
    if (!playing && tRef.current >= replay!.duration) set(0);
    setPlaying((p) => !p);
  }

  const pos: Record<Side, number> = { reference: distanceAt(replay, "reference", t), target: distanceAt(replay, "target", t) };
  const gap = gapAt(replay, t);
  const trend = trendAt(replay, t);
  const code: Record<Side, string> = { reference: comparison.meta.reference.driver, target: comparison.meta.target.driver };
  const trail = Math.min(pos.reference, pos.target);
  const leadColor = styles[gap.leader].color;
  const status = statusText(replay, t, gap, trend, code);

  return (
    <div data-testid="replay">
      <svg viewBox={`0 0 ${SIZE.width} ${SIZE.height}`} className="w-full" style={{ maxHeight }}
        role="img" aria-labelledby="replay-title replay-desc">
        <title id="replay-title">Lap replay: {code.reference} and {code.target} on track at their real pace</title>
        <desc id="replay-desc">Each dot is a driver in their team colour; the coloured stretch between them is the gap.</desc>
        <BaseLine track={track} palette={palette} />
        {/* corners both drivers have completed show where the gap changed (green = target gained, red = lost) */}
        {segments.map(({ f, d, style }) => (
          <path key={f.id} d={d} fill="none" stroke={f.segment!.end_m <= trail ? style.color : palette.line}
            strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" opacity={f.segment!.end_m <= trail ? 0.75 : 1}>
            <title>{`${f.label}: ${style.glyph} ${style.label}`}</title>
          </path>
        ))}
        {gap.metres > 1 && (
          <path d={segmentPath(track, trail, Math.max(pos.reference, pos.target))} fill="none" stroke={leadColor}
            strokeWidth={6} strokeLinecap="round" opacity={0.45} data-testid="replay-gap" />
        )}
        <StartFinish track={track} palette={palette} />
        {labels === "leaders" && <CornerLabels track={track} segments={segments} palette={palette} onPick={pickCorner} />}
        {(["target", "reference"] as const).map((side) => {
          const [x, y] = pointAt(track, pos[side]);
          const hollow = sameTeam && side === "target";
          return (
            <g key={side} transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`} pointerEvents="none"
              data-testid={`replay-dot-${side}`} data-distance={pos[side].toFixed(1)}>
              <circle r={7.5} fill={hollow ? palette.card : styles[side].color} stroke={hollow ? styles[side].color : "#ffffff"}
                strokeWidth={hollow ? 3.5 : 2} />
              <text y={side === gap.leader ? -11 : 17} textAnchor="middle" fontSize="9" fontWeight="800"
                fill={styles[side].text} stroke={palette.card} strokeWidth={3} paintOrder="stroke">{code[side]}</text>
            </g>
          );
        })}
      </svg>
      <div className="mt-2 flex flex-wrap items-center gap-2" role="group" aria-label="Replay controls">
        <button type="button" className="btn btn-ghost text-xs" onClick={togglePlay} data-testid="replay-play">
          {playing ? "❚❚ Pause" : t >= replay.duration ? "↺ Replay" : "▶ Play"}
        </button>
        <button type="button" className="btn btn-ghost text-xs" onClick={() => { stop(); setPlaying(false); setJump(null); set(0); }}
          data-testid="replay-reset">⏮ Start</button>
        <label className="flex items-center gap-1 text-xs text-muted">Speed
          <select className="rounded border border-line bg-card px-1 py-0.5 text-xs" value={speed} aria-label="Replay speed"
            data-testid="replay-speed"
            onChange={(e) => setSpeed(Number(e.target.value) as (typeof SPEEDS)[number])}>
            {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
          </select>
        </label>
        <input type="range" className="min-w-[140px] flex-1" min={0} max={replay.duration} step={0.05} value={t}
          aria-label="Replay time" aria-valuetext={`${t.toFixed(1)} s`} data-testid="replay-scrub"
          onChange={(e) => { stop(); setPlaying(false); setJump(null); set(Number(e.target.value)); }} />
        <span className="num-tab w-14 text-right text-xs text-muted">{t.toFixed(1)} s</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-xs">
        {(["reference", "target"] as const).map((side) => (
          <span key={side} className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full"
              style={sameTeam && side === "target"
                ? { border: `2.5px solid ${styles[side].color}` } : { background: styles[side].color }} />
            <b style={{ color: styles[side].text }}>{code[side]}</b>
            <span className="text-muted">{side === "reference" ? "reference" : "target"} · L{comparison.meta[side].lap_number}</span>
          </span>
        ))}
      </div>
      <p className="mt-2 rounded-lg border border-line bg-card-2 px-3 py-2 text-sm" data-testid="replay-status"
        aria-live={playing ? "off" : "polite"}>
        {jump ? jumpText(jump, code) : status}
      </p>
      <p className="mt-1 text-[11px] text-muted">Click a corner number: the driver who reaches it first moves there, the other stops where they were at that moment.</p>
      {labels === "leaders" && <CornerChips segments={segments} onPick={pickCorner} />}
    </div>
  );
}

function statusText(r: Replay, t: number, gap: ReturnType<typeof gapAt>, trend: string, code: Record<Side, string>): string {
  if (t <= 0) return `${code.reference} and ${code.target} start from the line. Press play.`;
  if (t >= r.duration) {
    const winner = r.lapTime.reference <= r.lapTime.target ? "reference" : "target";
    return `Lap complete: ${code[winner]} was ${gap.seconds.toFixed(3)} s faster.`;
  }
  if (gap.metres < 0.5) return `${code.reference} and ${code.target} are level.`;
  const lead = `${code[gap.leader]} leads by ${gap.seconds.toFixed(3)} s (${gap.metres.toFixed(0)} m)`;
  if (trend === "closing") return `${lead}; ${code[gap.trailer]} is closing in.`;
  if (trend === "pulling_away") return `${lead} and is pulling away.`;
  return `${lead}.`;
}

function jumpText({ f, j }: { f: Finding; j: CornerJump }, code: Record<Side, string>): string {
  if (j.gapSeconds < 0.0005) return `${f.label}: ${code.reference} and ${code.target} arrive together.`;
  return `${f.label}: ${code[j.faster]} gets there first; ${code[j.slower]} is ${j.gapMetres.toFixed(0)} m back ` +
    `and arrives ${j.gapSeconds.toFixed(3)} s later.`;
}

// ---------------------------------------------------------------- shared pieces
function svgPoint(e: React.MouseEvent<SVGSVGElement>): DOMPoint | null {
  const svg = e.currentTarget;
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  const m = svg.getScreenCTM();
  return m ? pt.matrixTransform(m.inverse()) : null;
}

function BaseLine({ track, palette }: { track: TrackGeometry; palette: ThemePalette }) {
  // pathLength="1" lets the CSS draw the circuit like a lap being traced (globals.css .track-draw)
  return <path d={segmentPath(track, 0, track.distance[track.distance.length - 1]!)} fill="none" pathLength={1}
    stroke={palette.line} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" className="track-draw" />;
}

/** Turn labels on leader lines: a short line leaves each apex, square to the track and away from the
 * circuit's middle, and the label sits at its end. Corners close together get longer lines. */
function CornerLabels({ track, segments, palette, onPick }: {
  track: TrackGeometry; segments: Seg[]; palette: ThemePalette; onPick: (f: Finding) => void;
}) {
  const selected = useUiStore((s) => s.selectedCornerId);
  const leaders = useMemo(() => leaderLayout(track, segments.map((sg) => sg.f)), [track, segments]);
  return segments.map(({ f, style }, i) => {
    const l = leaders[i]!;
    const isSel = f.id === selected;
    return (
      <g key={`${f.id}-label`} onClick={() => onPick(f)} className="track-seg cursor-pointer" aria-hidden
        data-testid={`map-label-${cornerShortLabel(f)}`}>
        <line x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2} stroke={isSel ? palette.accent : palette.muted}
          strokeWidth={isSel ? 1.6 : 1} />
        <circle cx={l.x1} cy={l.y1} r={1.8} fill={isSel ? palette.accent : style.color} />
        <text x={l.tx} y={l.ty} textAnchor={l.anchor} dominantBaseline="central" fontSize={LABEL_FONT}
          fontWeight={isSel ? 800 : 700} fill={isSel ? palette.accent : palette.ink}
          stroke={palette.card} strokeWidth={3} paintOrder="stroke">{f.corner_number ?? "•"}</text>
        {/* an invisible, generous hit area on top: the label text and hairline alone are hard to click */}
        <rect x={l.tx - l.w / 2 - 4} y={l.ty - l.h / 2 - 3} width={l.w + 8} height={l.h + 6} fill="transparent"
          pointerEvents="all" data-testid={`map-label-hit-${cornerShortLabel(f)}`} />
        <title>{`${f.label}: ${style.glyph} ${style.label}`}</title>
      </g>
    );
  });
}

interface Leader { x1: number; y1: number; x2: number; y2: number; tx: number; ty: number; w: number; h: number; anchor: "middle" }

const LABEL_FONT = 9.5;
const TRACK_CLEAR = 8;        // keep labels this far from the track's centre line

/**
 * Where each corner's leader line and label go (SVG units). Greedy, corner by corner: try directions
 * (square to the track and away from the circuit's middle first, then angled, then inwards) and
 * lengths, and take the first spot where the label is inside the map, clear of the track and clear
 * of every label already placed.
 */
export function leaderLayout(track: TrackGeometry, corners: Finding[]): Leader[] {
  const n = track.px.length;
  const step = Math.max(1, Math.floor(n / 900));          // the track polyline, thinned for the checks
  const tx: number[] = [], ty: number[] = [];
  for (let k = 0; k < n; k += step) { tx.push(track.px[k]!); ty.push(track.py[k]!); }
  const cx = tx.reduce((a, b) => a + b, 0) / tx.length;
  const cy = ty.reduce((a, b) => a + b, 0) / ty.length;
  const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];

  const clearOfTrack = (b: { x0: number; y0: number; x1: number; y1: number }) => {
    for (let k = 0; k < tx.length; k++) {
      const dx = Math.max(b.x0 - tx[k]!, 0, tx[k]! - b.x1), dy = Math.max(b.y0 - ty[k]!, 0, ty[k]! - b.y1);
      if (dx * dx + dy * dy < TRACK_CLEAR * TRACK_CLEAR) return false;
    }
    return true;
  };
  const clearOfLabels = (b: { x0: number; y0: number; x1: number; y1: number }) =>
    placed.every((o) => b.x1 + 2 < o.x0 || o.x1 + 2 < b.x0 || b.y1 + 1 < o.y0 || o.y1 + 1 < b.y0);
  const inside = (b: { x0: number; y0: number; x1: number; y1: number }) =>
    b.x0 >= 1 && b.y0 >= 1 && b.x1 <= track.width - 1 && b.y1 <= track.height - 1;

  return corners.map((f) => {
    const d = f.segment!.apex_m ?? f.segment!.start_m;
    const [x, y] = pointAt(track, d);
    const [ax, ay] = pointAt(track, d - 20), [bx, by] = pointAt(track, d + 20);
    let nx = -(by - ay), ny = bx - ax;                       // normal to the track's direction
    const len = Math.hypot(nx, ny) || 1;
    nx /= len; ny /= len;
    if (nx * (x - cx) + ny * (y - cy) < 0) { nx = -nx; ny = -ny; }   // away from the middle
    const text = String(f.corner_number ?? "•");
    const w = text.length * LABEL_FONT * 0.62 + 2, h = LABEL_FONT + 2;

    let best: Leader | null = null;
    search:
    for (const reach of [16, 26, 38, 52]) {
      for (const angle of [0, 30, -30, 60, -60, 90, -90, 180, 150, -150, 120, -120]) {
        const t = (angle * Math.PI) / 180;
        const dx = nx * Math.cos(t) - ny * Math.sin(t), dy = nx * Math.sin(t) + ny * Math.cos(t);
        const ex = x + dx * reach, ey = y + dy * reach;
        // the label's centre sits just beyond the line's end, along the same direction
        const lx = ex + dx * (w / 2 + 2), ly = ey + dy * (h / 2 + 2);
        const box = { x0: lx - w / 2, y0: ly - h / 2, x1: lx + w / 2, y1: ly + h / 2 };
        if (!inside(box) || !clearOfLabels(box) || !clearOfTrack(box)) continue;
        placed.push(box);
        best = { x1: round(x + dx * 7), y1: round(y + dy * 7), x2: round(ex), y2: round(ey), tx: round(lx), ty: round(ly), w: round(w), h: round(h), anchor: "middle" };
        break search;
      }
    }
    if (!best) {                                              // nothing clear: the plain outward spot
      const ex = x + nx * 26, ey = y + ny * 26;
      best = { x1: round(x + nx * 7), y1: round(y + ny * 7), x2: round(ex), y2: round(ey),
        tx: round(ex + nx * (w / 2 + 2)), ty: round(ey + ny * (h / 2 + 2)), w: round(w), h: round(h), anchor: "middle" };
    }
    return best;
  });
}

const round = (v: number) => Math.round(v * 10) / 10;

function CornerChips({ segments, onPick }: { segments: Seg[]; onPick: (f: Finding) => void }) {
  const selected = useUiStore((s) => s.selectedCornerId);
  return (
    <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label="Corners">
      {segments.map(({ f, style }) => (
        <button key={f.id} type="button" onClick={() => onPick(f)} aria-pressed={f.id === selected}
          title={`${f.label}: ${style.label}`}
          data-testid={`corner-${cornerShortLabel(f)}`}
          className={`chip cursor-pointer ${f.id === selected ? "ring-2 ring-[var(--accent)]" : ""}`}>
          <span aria-hidden style={{ color: style.color }}>{style.glyph}</span>
          {cornerShortLabel(f)}
          <span className="num-tab text-muted">{formatDelta(f.time_loss_s, { withWord: false, digits: 2 }).replace(" s", "")}</span>
          <span className="sr-only">{`s, ${f.label}: target ${style.label}`}</span>
        </button>
      ))}
    </div>
  );
}

/** The start/finish line: a white hash across the track (dark outline so it shows on light themes). */
function StartFinish({ track, palette }: { track: TrackGeometry; palette: ThemePalette }) {
  const [x, y] = pointAt(track, 0);
  const [x2, y2] = pointAt(track, Math.min(15, track.distance[track.distance.length - 1]!));
  const len = Math.hypot(x2 - x, y2 - y) || 1;
  const nx = -(y2 - y) / len, ny = (x2 - x) / len;      // unit normal to the direction of travel
  const h = 9;
  const line = { x1: x - nx * h, y1: y - ny * h, x2: x + nx * h, y2: y + ny * h };
  return (
    <g aria-hidden data-testid="start-finish">
      <line {...line} stroke={palette.ink} strokeWidth={4} strokeLinecap="round" opacity={0.55} />
      <line {...line} stroke="#ffffff" strokeWidth={2} strokeLinecap="round" />
      <title>Start / finish</title>
    </g>
  );
}

/** Follows the shared hover distance (from any chart or the map itself). */
