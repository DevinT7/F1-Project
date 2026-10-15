/**
 * Shareable URL state: ?s=2024-05-Q&ref=NOR:fastest&tgt=VER:15[&res=4][&tyre=NOR&asof=31][&tab=track][&demo=1]
 * The page renders from the URL alone; every selection writes back to it.
 */

import { DEFAULT_TAB, parseTab, type TabId } from "./tabs";

export type LapChoice = number | "fastest";
export interface LapRef {
  driver: string;
  lap: LapChoice;
}
export interface UrlState {
  session: string | null;
  ref: LapRef | null;
  tgt: LapRef | null;
  resolution: number;
  demo: boolean;
  tyre?: string | null;      // tyre panel driver
  asof?: number | null;      // tyre panel "as of" lap (the forecast uses laps ≤ asof only)
  tab?: TabId;               // the top-level tab (default: Pit Wall)
}
export interface SessionParts {
  year: number;
  round: number;
  code: string;
}

export const DEFAULT_RESOLUTION = 2;
const SESSION_RE = /^(\d{4})[-_](\d{2})[-_]([A-Za-z0-9]{1,3})$/;
const SESSION_CODES = ["FP1", "FP2", "FP3", "SQ", "SS", "S", "Q", "R"] as const;
const DRIVER_RE = /^[A-Za-z0-9]{1,4}$/;

export const emptyState: UrlState = {
  session: null, ref: null, tgt: null, resolution: DEFAULT_RESOLUTION, demo: false, tyre: null, asof: null, tab: DEFAULT_TAB,
};

export function normalizeSessionKey(raw: string | null | undefined): string | null {
  const m = SESSION_RE.exec((raw ?? "").trim());
  if (!m) return null;
  const code = m[3]!.toUpperCase();
  if (!(SESSION_CODES as readonly string[]).includes(code)) return null;
  return `${m[1]}-${m[2]}-${code}`;
}

export function sessionKey(parts: SessionParts): string {
  return `${parts.year}-${String(parts.round).padStart(2, "0")}-${parts.code.toUpperCase()}`;
}

export function parseSessionKey(key: string | null | undefined): SessionParts | null {
  const normalized = normalizeSessionKey(key);
  if (!normalized) return null;
  const [year, round, code] = normalized.split("-");
  return { year: Number(year), round: Number(round), code: code! };
}

export function parseLapRef(raw: string | null | undefined): LapRef | null {
  if (!raw) return null;
  const [driver, lap = "fastest", ...rest] = raw.trim().split(":");
  if (rest.length || !driver || !DRIVER_RE.test(driver)) return null;
  if (lap.toLowerCase() === "fastest") return { driver: driver.toUpperCase(), lap: "fastest" };
  if (!/^\d{1,3}$/.test(lap) || Number(lap) < 1) return null;
  return { driver: driver.toUpperCase(), lap: Number(lap) };
}

export function formatLapRef(ref: LapRef): string {
  return `${ref.driver}:${ref.lap}`;
}

export function parseUrlState(search: string | URLSearchParams): UrlState {
  const params = typeof search === "string" ? new URLSearchParams(search) : search;
  const res = Number(params.get("res"));
  const tyre = params.get("tyre")?.trim().toUpperCase() ?? "";
  const asofRaw = params.get("asof") ?? "";
  const asof = /^\d{1,3}$/.test(asofRaw) && Number(asofRaw) >= 1 ? Number(asofRaw) : null;
  return {
    session: normalizeSessionKey(params.get("s")),
    ref: parseLapRef(params.get("ref")),
    tgt: parseLapRef(params.get("tgt")),
    resolution: Number.isFinite(res) && res >= 1 && res <= 10 ? res : DEFAULT_RESOLUTION,
    demo: params.get("demo") === "1",
    tyre: DRIVER_RE.test(tyre) ? tyre : null,
    asof,
    tab: parseTab(params.get("tab")),
  };
}

/** Canonical query string (fixed key order, defaults omitted) so equal states give equal URLs. */
export function serializeUrlState(state: UrlState): string {
  const params = new URLSearchParams();
  if (state.session) params.set("s", state.session);
  if (state.ref) params.set("ref", formatLapRef(state.ref));
  if (state.tgt) params.set("tgt", formatLapRef(state.tgt));
  if (state.resolution !== DEFAULT_RESOLUTION) params.set("res", String(state.resolution));
  if (state.tyre) params.set("tyre", state.tyre);
  if (state.tyre && state.asof) params.set("asof", String(state.asof));
  if (state.tab && state.tab !== DEFAULT_TAB) params.set("tab", state.tab);
  if (state.demo) params.set("demo", "1");
  return params.toString().replace(/%3A/gi, ":");
}

export function isComparable(state: UrlState): state is UrlState & { session: string; ref: LapRef; tgt: LapRef } {
  return Boolean(state.session && state.ref && state.tgt);
}
