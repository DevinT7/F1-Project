/**
 * Display formatting. Sign convention (matches the API): delta / time loss = target − reference,
 * so POSITIVE means the target is SLOWER (lost time) and NEGATIVE means FASTER.
 * Missing values render as an em dash, never as 0.
 */

export const MISSING = "—";
const MINUS = "−"; // U+2212, typographic minus

type Num = number | null | undefined;

function ok(value: Num): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function signed(value: number, digits: number): string {
  const rounded = Number(value.toFixed(digits));
  const magnitude = Math.abs(rounded).toFixed(digits);
  if (rounded > 0) return `+${magnitude}`;
  if (rounded < 0) return `${MINUS}${magnitude}`;
  return `±${magnitude}`;
}

/** formatDelta(+0.213) → "+0.213 s (slower)"; formatDelta(-0.1) → "−0.100 s (faster)". */
export function formatDelta(seconds: Num, opts: { digits?: number; withWord?: boolean } = {}): string {
  if (!ok(seconds)) return MISSING;
  const digits = opts.digits ?? 3;
  const text = `${signed(seconds, digits)} s`;
  if (opts.withWord === false) return text;
  const rounded = Number(seconds.toFixed(digits));
  const word = rounded > 0 ? "slower" : rounded < 0 ? "faster" : "level";
  return `${text} (${word})`;
}

/** 93.66 → "1:33.660" */
export function formatLapTime(seconds: Num): string {
  if (!ok(seconds) || seconds < 0) return MISSING;
  const totalMs = Math.round(seconds * 1000);
  const minutes = Math.floor(totalMs / 60000);
  const rest = (totalMs - minutes * 60000) / 1000;
  return `${minutes}:${rest.toFixed(3).padStart(6, "0")}`;
}

export function formatSpeed(kph: Num, digits = 1): string {
  return ok(kph) ? `${kph.toFixed(digits)} km/h` : MISSING;
}

export function formatDistance(metres: Num): string {
  return ok(metres) ? `${Math.round(metres).toLocaleString("en-US")} m` : MISSING;
}

export function formatSeconds(seconds: Num, digits = 3): string {
  return ok(seconds) ? `${seconds.toFixed(digits)} s` : MISSING;
}

export function formatPercent(pct: Num, digits = 0): string {
  return ok(pct) ? `${pct.toFixed(digits)}%` : MISSING;
}

export function formatGear(gear: Num): string {
  return ok(gear) && gear > 0 ? String(Math.round(gear)) : MISSING;
}

/** A target − reference difference with unit: "+9.6 km/h", "−18 m", "±0.0 km/h". */
export function formatDiff(value: Num, unit: string, digits = 1): string {
  return ok(value) ? `${signed(value, digits)}${unit ? ` ${unit}` : ""}` : MISSING;
}

export function formatTemperature(celsius: Num): string {
  return ok(celsius) ? `${celsius.toFixed(1)} °C` : MISSING;
}

/** Plain-language explanation of the gap for tooltips. */
export function describeGap(seconds: Num, target: string, reference: string): string {
  if (!ok(seconds)) return "Gap unavailable.";
  const rounded = Number(seconds.toFixed(3));
  if (rounded === 0) return `${target} and ${reference} set identical lap times.`;
  const magnitude = Math.abs(rounded).toFixed(3);
  return rounded > 0
    ? `${target} was ${magnitude} s slower than ${reference}. Positive values always mean the target lost time.`
    : `${target} was ${magnitude} s faster than ${reference}. Negative values always mean the target gained time.`;
}
