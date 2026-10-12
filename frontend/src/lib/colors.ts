/**
 * Colour logic: the diverging time-loss scale (shared by the track map and findings) and
 * WCAG-based contrast adjustment for team colours on dark/light backgrounds.
 */

export const NOISE_FLOOR_S = 0.02;  // same as the backend's Thresholds.noise_floor_s
const SATURATE_AT_S = 0.15;         // |loss| at which the colour reaches full strength

export const SCALE = {
  gain: "#3f9d6d",    // the existing pit-wall --green
  loss: "#d1543f",    // the existing pit-wall --red
  neutral: "#8a8683", // the existing pit-wall --muted
} as const;

export type LossKind = "gain" | "loss" | "neutral";
export interface LossStyle {
  kind: LossKind;
  color: string;
  glyph: "▲" | "▼" | "●";      // shape cue so colour is never the only signal
  intensity: number;            // 0..1
  label: string;                // "lost 0.171 s" / "gained 0.094 s" / "level (< 0.02 s)"
}

/** time loss (target − reference, s) → colour. |loss| < 0.02 s is neutral (below the delta noise floor). */
export function lossStyle(timeLossS: number | null | undefined): LossStyle {
  if (timeLossS === null || timeLossS === undefined || !Number.isFinite(timeLossS) || Math.abs(timeLossS) < NOISE_FLOOR_S) {
    return { kind: "neutral", color: SCALE.neutral, glyph: "●", intensity: 0, label: `level (< ${NOISE_FLOOR_S} s)` };
  }
  const intensity = Math.min(1, (Math.abs(timeLossS) - NOISE_FLOOR_S) / (SATURATE_AT_S - NOISE_FLOOR_S));
  const kind: LossKind = timeLossS > 0 ? "loss" : "gain";
  const base = kind === "loss" ? SCALE.loss : SCALE.gain;
  return {
    kind,
    color: mix(SCALE.neutral, base, 0.45 + 0.55 * intensity),
    glyph: kind === "loss" ? "▲" : "▼",
    intensity,
    label: `${kind === "loss" ? "lost" : "gained"} ${Math.abs(timeLossS).toFixed(3)} s`,
  };
}

// ---------------------------------------------------------------- colour maths
export function parseHex(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const h = m[1]!.length === 3 ? m[1]!.split("").map((c) => c + c).join("") : m[1]!;
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export function toHex([r, g, b]: [number, number, number]): string {
  return `#${[r, g, b].map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("")}`;
}

export function mix(a: string, b: string, t: number): string {
  const ca = parseHex(a)!, cb = parseHex(b)!;
  return toHex([0, 1, 2].map((i) => ca[i]! + (cb[i]! - ca[i]!) * t) as [number, number, number]);
}

/** WCAG relative luminance. */
export function luminance(hex: string): number {
  const rgb = parseHex(hex);
  if (!rgb) return 0;
  const [r, g, b] = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Lighten (on dark backgrounds) or darken (on light ones) a team colour until it reaches
 * `minRatio` against the background. 3:1 is the WCAG minimum for graphics such as lines.
 * Colours that already pass are returned unchanged (normalized to #rrggbb).
 */
export function ensureContrast(color: string | null | undefined, background: string, minRatio = 3): string {
  const fallback = luminance(background) < 0.5 ? "#e6e2df" : "#403c3a";
  if (!color || !parseHex(color)) return fallback;
  const start = toHex(parseHex(color)!);
  if (contrastRatio(start, background) >= minRatio) return start;
  const toward = luminance(background) < 0.5 ? "#ffffff" : "#000000";
  for (let t = 0.05; t <= 1.0001; t += 0.05) {
    const candidate = mix(start, toward, t);
    if (contrastRatio(candidate, background) >= minRatio) return candidate;
  }
  return toward;
}

/** Team colours are often equal (team-mates): the target then gets a dashed line. */
export function sameTeamColor(a: string | null | undefined, b: string | null | undefined): boolean {
  return Boolean(a && b && parseHex(a) && parseHex(b) && toHex(parseHex(a)!) === toHex(parseHex(b)!));
}
