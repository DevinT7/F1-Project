/**
 * Lap replay maths for the track map. Both laps are aligned to the reference lap's distance grid,
 * and each carries its elapsed lap time at every grid distance (`channels.*.time_s`), so a
 * driver's position at replay time t is the distance where its elapsed time equals t (inverting
 * time(distance)). Both dots run on the reference line; neither ever uses data the other lacks.
 */

export type Side = "reference" | "target";

export interface Replay {
  distance: number[];          // metres, increasing (the comparison grid)
  time: Record<Side, number[]>; // elapsed time at each distance, made non-decreasing
  lapTime: Record<Side, number>;
  duration: number;            // the slower lap: the replay ends when both have crossed the line
}

function monotone(values: number[]): number[] {
  const out: number[] = [];
  let best = -Infinity;
  for (const v of values) {
    best = Math.max(best, Number.isFinite(v) ? v : best);
    out.push(best === -Infinity ? 0 : best);
  }
  return out;
}

export function buildReplay(distance: number[], refTime: number[], tgtTime: number[]): Replay | null {
  const n = Math.min(distance.length, refTime.length, tgtTime.length);
  if (n < 2) return null;
  const time = { reference: monotone(refTime.slice(0, n)), target: monotone(tgtTime.slice(0, n)) };
  const lapTime = { reference: time.reference[n - 1]!, target: time.target[n - 1]! };
  return { distance: distance.slice(0, n), time, lapTime, duration: Math.max(lapTime.reference, lapTime.target) };
}

/** Interpolate ys at x over increasing xs (clamped at both ends). */
function interp(xs: number[], ys: number[], x: number): number {
  const last = xs.length - 1;
  if (x <= xs[0]!) return ys[0]!;
  if (x >= xs[last]!) return ys[last]!;
  let lo = 0, hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid]! <= x) lo = mid; else hi = mid;
  }
  const span = xs[hi]! - xs[lo]!;
  return span > 0 ? ys[lo]! + ((ys[hi]! - ys[lo]!) * (x - xs[lo]!)) / span : ys[hi]!;
}

/** Where `side` is on the lap at replay time t (stays on the line once its lap is done). */
export function distanceAt(r: Replay, side: Side, t: number): number {
  return interp(r.time[side], r.distance, t);
}

/** When `side` reached distance d. */
export function timeAt(r: Replay, side: Side, d: number): number {
  return interp(r.distance, r.time[side], d);
}

export interface Gap {
  leader: Side;
  trailer: Side;
  seconds: number;     // how long ago the leader was where the trailer is now
  metres: number;      // how far ahead the leader is on track
}

export function gapAt(r: Replay, t: number): Gap {
  const dRef = distanceAt(r, "reference", t), dTgt = distanceAt(r, "target", t);
  const leader: Side = dRef > dTgt || (dRef === dTgt && r.lapTime.reference <= r.lapTime.target) ? "reference" : "target";
  const trailer: Side = leader === "reference" ? "target" : "reference";
  const dTrail = Math.min(dRef, dTgt);
  const finished = (s: Side) => t >= r.lapTime[s];
  // after both finish, the gap is the lap-time difference
  const seconds = finished("reference") && finished("target")
    ? Math.abs(r.lapTime.reference - r.lapTime.target)
    : Math.max(0, Math.min(t, r.lapTime[trailer]) - timeAt(r, leader, dTrail));
  return { leader, trailer, seconds, metres: Math.abs(dRef - dTgt) };
}

/** Is the trailer closing in (gap shrinking) or dropping back, over the last `window` seconds? */
export function trendAt(r: Replay, t: number, window = 1): "closing" | "pulling_away" | "steady" {
  if (t <= window) return "steady";
  const now = gapAt(r, t), before = gapAt(r, t - window);
  if (now.leader !== before.leader) return "closing";
  const change = now.seconds - before.seconds;
  if (Math.abs(change) < 0.005) return "steady";
  return change < 0 ? "closing" : "pulling_away";
}

export interface CornerJump {
  t: number;           // replay time: the moment the faster driver reaches the corner
  faster: Side;
  slower: Side;
  slowerDistance: number;
  gapSeconds: number;  // how much later the slower driver reaches the corner
  gapMetres: number;   // how far behind the slower driver is at that moment
}

/** The faster driver at the corner; the slower one where they were at that same instant. */
export function jumpToCorner(r: Replay, cornerM: number): CornerJump {
  const tRef = timeAt(r, "reference", cornerM), tTgt = timeAt(r, "target", cornerM);
  const faster: Side = tRef <= tTgt ? "reference" : "target";
  const slower: Side = faster === "reference" ? "target" : "reference";
  const t = Math.min(tRef, tTgt);
  const slowerDistance = distanceAt(r, slower, t);
  return { t, faster, slower, slowerDistance, gapSeconds: Math.abs(tRef - tTgt), gapMetres: Math.max(0, cornerM - slowerDistance) };
}

export const SPEEDS = [1, 2, 4, 8] as const;
export const JUMP_MS = 700;

/** Ease for the corner jump animation. */
export function easeInOut(x: number): number {
  return x < 0.5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2;
}
