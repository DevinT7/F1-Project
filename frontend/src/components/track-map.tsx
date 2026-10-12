"use client";
/**
 * SVG circuit from the reference lap's x/y, split into the Phase 3 corner segments and coloured
 * by segment time loss (green = target gained, red = target lost, grey = |loss| < 0.02 s).
 * Colour is never the only cue: each corner carries ▲/▼/● and its magnitude in a tooltip, and
 * every corner is a keyboard-focusable button.
 */
import { useMemo } from "react";

import type { ComparisonResponse, CornersResponse, Finding } from "@/lib/api/endpoints";
import { useThemePalette } from "@/lib/chart-colors";
import { lossStyle, NOISE_FLOOR_S } from "@/lib/colors";
import { formatDelta } from "@/lib/format";
import { useUiStore } from "@/lib/store";
import { buildTrack, nearestDistance, pointAt, segmentPath, type TrackGeometry } from "@/lib/track";

const SIZE = { width: 420, height: 320, padding: 22 };

export function cornerShortLabel(f: Finding): string {
  return f.corner_number != null ? `T${f.corner_number}` : "•";
}

export function TrackMap({ comparison, corners }: { comparison: ComparisonResponse; corners: CornersResponse | undefined }) {
  const palette = useThemePalette();
  const selected = useUiStore((s) => s.selectedCornerId);
  const selectCorner = useUiStore((s) => s.selectCorner);
  const setHover = useUiStore((s) => s.setHover);
  const ref = comparison.channels.reference;
  const track = useMemo(
    () => buildTrack(comparison.distance_m, ref.x, ref.y, corners?.circuit_rotation_deg ?? 0, SIZE),
    [comparison, ref.x, ref.y, corners?.circuit_rotation_deg],
  );
  const findings = comparison.findings.corners;
  const segments = useMemo(
    () => (track ? findings.map((f) => ({ f, d: segmentPath(track, f.segment!.start_m, f.segment!.end_m), style: lossStyle(f.time_loss_s) })) : []),
    [track, findings],
  );

  if (!track) {
    return <p className="text-sm text-muted">No position data for this lap, so the map can&apos;t be drawn.</p>;
  }

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const svg = e.currentTarget;
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const m = svg.getScreenCTM();
    if (!m) return;
    const local = pt.matrixTransform(m.inverse());
    setHover(nearestDistance(track!, local.x, local.y));
  }

  return (
    <div>
      <svg viewBox={`0 0 ${SIZE.width} ${SIZE.height}`} className="w-full" style={{ maxHeight: 340 }}
        role="img" aria-labelledby="track-title track-desc" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <title id="track-title">Track map coloured by where the target gained or lost time</title>
        <desc id="track-desc">Each corner segment is green when the target gained time, red when it lost time, and grey within ±{NOISE_FLOOR_S} s.</desc>
        <path d={segmentPath(track, 0, track.distance[track.distance.length - 1]!)} fill="none"
          stroke={palette.line} strokeWidth={13} strokeLinecap="round" strokeLinejoin="round" />
        {segments.map(({ f, d, style }) => (
          <path key={f.id} d={d} fill="none" stroke={style.color}
            strokeWidth={f.id === selected ? 9 : 6} strokeLinecap="round" strokeLinejoin="round"
            opacity={selected && f.id !== selected ? 0.55 : 1} onClick={() => selectCorner(f.id)} className="cursor-pointer">
            <title>{`${f.label}: ${style.glyph} ${style.label}`}</title>
          </path>
        ))}
        <StartFinish track={track} color={palette.ink} />
        {segments.map(({ f, style }) => {
          const [x, y] = pointAt(track, f.segment!.apex_m ?? f.segment!.start_m);
          const isSel = f.id === selected;
          return (
            <g key={`${f.id}-label`} transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`} onClick={() => selectCorner(f.id)}
              className="cursor-pointer" aria-hidden>
              <circle r={isSel ? 11 : 9} fill={palette.card} stroke={isSel ? palette.accent : style.color} strokeWidth={isSel ? 3 : 2} />
              <text textAnchor="middle" dy="3.2" fontSize="8.5" fontWeight="700" fill={palette.ink}>
                {f.corner_number ?? "•"}
              </text>
              <title>{`${f.label}: ${style.glyph} ${style.label}`}</title>
            </g>
          );
        })}
        <HoverMarker track={track} color={palette.accent} />
      </svg>
      <ul className="mt-2 flex flex-wrap justify-center gap-4 text-xs text-muted" aria-label="Map legend">
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(-0.2).color }} />▼ Target gaining</li>
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(0.2).color }} />▲ Target losing</li>
        <li><span aria-hidden className="mr-1.5 inline-block h-1 w-4 rounded align-middle" style={{ background: lossStyle(0).color }} />● Level (&lt; {NOISE_FLOOR_S} s)</li>
      </ul>
      <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label="Corners">
        {segments.map(({ f, style }) => (
          <button key={f.id} type="button" onClick={() => selectCorner(f.id)} aria-pressed={f.id === selected}
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
    </div>
  );
}

function StartFinish({ track, color }: { track: TrackGeometry; color: string }) {
  const [x, y] = pointAt(track, 0);
  return <rect x={x - 3} y={y - 3} width={6} height={6} fill={color} aria-hidden><title>Start / finish</title></rect>;
}

/** Follows the shared hover distance (from any chart or the map itself). */
function HoverMarker({ track, color }: { track: TrackGeometry; color: string }) {
  const hover = useUiStore((s) => s.hoverDistance);
  if (hover === null) return null;
  const [x, y] = pointAt(track, hover);
  return <circle data-testid="map-marker" cx={x} cy={y} r={6} fill={color} stroke="#fff" strokeWidth={2} pointerEvents="none" />;
}
