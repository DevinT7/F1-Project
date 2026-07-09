// API client (Person 4). Talks to the FastAPI backend. In dev, Vite proxies /api.
// Set VITE_API_BASE to point at a deployed backend in prod.
import type {
  AIReport, Driver, LapComparison, Session, TelemetryTrace, TireAnalysis,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path} → ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  sessions: (year: number, country?: string) =>
    get<Session[]>(`/api/sessions?year=${year}${country ? `&country=${country}` : ""}`),

  drivers: (sessionKey: number) =>
    get<Driver[]>(`/api/sessions/${sessionKey}/drivers`),

  telemetry: (sessionKey: number, driver: number, lap?: number) =>
    get<TelemetryTrace>(
      `/api/telemetry?session_key=${sessionKey}&driver_number=${driver}` +
        (lap ? `&lap_number=${lap}` : "")),

  compare: (sessionKey: number, driver: number, reference: number) =>
    get<LapComparison>(
      `/api/analysis/compare?session_key=${sessionKey}` +
        `&driver_number=${driver}&reference_driver_number=${reference}`),

  tires: (sessionKey: number, driver: number) =>
    get<TireAnalysis>(
      `/api/tires/degradation?session_key=${sessionKey}&driver_number=${driver}`),

  aiReport: (comparison: LapComparison, tires: TireAnalysis, question?: string) =>
    post<AIReport>("/api/ai/report", { comparison, tires, question }),
};
