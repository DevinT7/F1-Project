/**
 * Track-map geometry from the reference lap's x/y (FastF1 decimetres) along the distance grid.
 * Rotation follows FastF1's circuit_info.rotation convention (counter-clockwise, degrees);
 * SVG's y axis points down, so y is flipped when fitting.
 */

export interface TrackGeometry {
  distance: number[];          // metres, increasing
  px: number[];                // SVG coordinates
  py: number[];
  width: number;
  height: number;
}

export function rotate(x: number, y: number, degrees: number): [number, number] {
  const t = (degrees * Math.PI) / 180;
  const c = Math.cos(t), s = Math.sin(t);
  return [x * c - y * s, x * s + y * c];
}

export function buildTrack(
  distance: number[],
  x: (number | null)[],
  y: (number | null)[],
  rotationDeg: number | null | undefined,
  size = { width: 400, height: 300, padding: 18 },
): TrackGeometry | null {
  const d: number[] = [], rx: number[] = [], ry: number[] = [];
  for (let i = 0; i < distance.length; i++) {
    const xi = x[i], yi = y[i], di = distance[i];
    if (xi == null || yi == null || di == null || !Number.isFinite(xi) || !Number.isFinite(yi)) continue;
    const [a, b] = rotate(xi, yi, rotationDeg ?? 0);
    d.push(di); rx.push(a); ry.push(b);
  }
  if (d.length < 2) return null;
  const minX = Math.min(...rx), maxX = Math.max(...rx), minY = Math.min(...ry), maxY = Math.max(...ry);
  const spanX = Math.max(maxX - minX, 1e-9), spanY = Math.max(maxY - minY, 1e-9);
  const inner = { w: size.width - 2 * size.padding, h: size.height - 2 * size.padding };
  const scale = Math.min(inner.w / spanX, inner.h / spanY);
  const offX = size.padding + (inner.w - spanX * scale) / 2;
  const offY = size.padding + (inner.h - spanY * scale) / 2;
  return {
    distance: d,
    px: rx.map((v) => offX + (v - minX) * scale),
    py: ry.map((v) => offY + (maxY - v) * scale), // flip: SVG y grows downward
    width: size.width,
    height: size.height,
  };
}

/** Interpolated SVG point at a distance (clamped to the lap). */
export function pointAt(track: TrackGeometry, d: number): [number, number] {
  const { distance, px, py } = track;
  if (d <= distance[0]!) return [px[0]!, py[0]!];
  const last = distance.length - 1;
  if (d >= distance[last]!) return [px[last]!, py[last]!];
  let lo = 0, hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (distance[mid]! <= d) lo = mid; else hi = mid;
  }
  const t = (d - distance[lo]!) / (distance[hi]! - distance[lo]!);
  return [px[lo]! + (px[hi]! - px[lo]!) * t, py[lo]! + (py[hi]! - py[lo]!) * t];
}

const r = (v: number) => Math.round(v * 10) / 10;

/**
 * SVG path for the stretch [startM, endM] of the lap. Endpoints are interpolated so adjacent
 * segments meet exactly; consecutive segments that tile the lap reproduce the whole polyline.
 */
export function segmentPath(track: TrackGeometry, startM: number, endM: number): string {
  if (!(endM > startM)) return "";
  const pts: [number, number][] = [pointAt(track, startM)];
  for (let i = 0; i < track.distance.length; i++) {
    const di = track.distance[i]!;
    if (di > startM && di < endM) pts.push([track.px[i]!, track.py[i]!]);
  }
  pts.push(pointAt(track, endM));
  return pts.map(([a, b], i) => `${i === 0 ? "M" : "L"}${r(a)} ${r(b)}`).join(" ");
}

/** Nearest grid distance to an SVG point (for hovering the map). */
export function nearestDistance(track: TrackGeometry, sx: number, sy: number): number {
  let best = 0, bestD2 = Infinity;
  for (let i = 0; i < track.distance.length; i++) {
    const dx = track.px[i]! - sx, dy = track.py[i]! - sy, d2 = dx * dx + dy * dy;
    if (d2 < bestD2) { bestD2 = d2; best = i; }
  }
  return track.distance[best]!;
}
