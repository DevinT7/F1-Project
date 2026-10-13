/**
 * One function per /api/v1 endpoint. In Demo mode (the old dashboard's "Demo data" button,
 * `?demo=1`) the same functions resolve from real API responses exported by
 * backend/scripts/export_frontend_fixtures.py, so the UI runs with no backend at all.
 */
import { ApiError, apiGet, apiPost, type Schemas } from "./client";

export type SeasonsResponse = Schemas["SeasonsResponse"];
export type EventsResponse = Schemas["EventsResponse"];
export type EventInfo = Schemas["EventInfo"];
export type SessionStatus = Schemas["SessionStatusResponse"];
export type IngestResponse = Schemas["IngestResponse"];
export type DriversResponse = Schemas["DriversResponse"];
export type DriverInfo = Schemas["DriverInfo"];
export type LapsResponse = Schemas["LapsResponse"];
export type LapRecord = Schemas["LapRecord"];
export type FastestLapsResponse = Schemas["FastestLapsResponse"];
export type CornersResponse = Schemas["CornersResponse"];
export type ComparisonRequest = Schemas["ComparisonRequest"];
export type ComparisonResponse = Schemas["ComparisonResponse"];
export type FindingsReport = Schemas["FindingsReport"];
export type Finding = Schemas["Finding"];
export type LapChannels = Schemas["LapChannels"];
export type StintsResponse = Schemas["StintsResponse"];
export type TirePrediction = Schemas["TirePrediction"];

export const DEMO_SESSION = "2024-05-Q";
export const DEMO_REFERENCE = { driver: "VER", lap: "fastest" } as const;
export const DEMO_TARGET = { driver: "ALO", lap: "fastest" } as const;

type DemoName =
  | "seasons" | "events_2024" | "session_2024-05-Q" | "drivers" | "laps"
  | "fastest_laps" | "corners" | "comparison_VER_ALO";

const demoLoaders: Record<DemoName, () => Promise<{ default: unknown }>> = {
  seasons: () => import("@/fixtures/api/seasons.json"),
  events_2024: () => import("@/fixtures/api/events_2024.json"),
  "session_2024-05-Q": () => import("@/fixtures/api/session_2024-05-Q.json"),
  drivers: () => import("@/fixtures/api/drivers.json"),
  laps: () => import("@/fixtures/api/laps.json"),
  fastest_laps: () => import("@/fixtures/api/fastest_laps.json"),
  corners: () => import("@/fixtures/api/corners.json"),
  comparison_VER_ALO: () => import("@/fixtures/api/comparison_VER_ALO.json"),
};

async function demo<T>(name: DemoName): Promise<T> {
  return (await demoLoaders[name]()).default as T;
}

/** A rejected promise (never a synchronous throw), so callers and TanStack Query see a normal failure. */
function demoOnly(what: string): Promise<never> {
  return Promise.reject(new ApiError(404, "demo_unavailable",
    `Demo data covers ${DEMO_SESSION} (VER, PER, ALO) and the VER vs ALO fastest-lap comparison only; ${what}.`));
}

function isDemoSession(key: string): boolean {
  return key.replace(/_/g, "-").toUpperCase() === DEMO_SESSION;
}

function lapLabel(lap: number | "fastest" | undefined): string {
  return lap === undefined ? "fastest" : String(lap);
}

export function createApi(demoMode: boolean) {
  return {
    seasons: (): Promise<SeasonsResponse> =>
      demoMode ? demo("seasons") : apiGet("/api/v1/seasons"),

    events: (year: number): Promise<EventsResponse> =>
      demoMode
        ? year === 2024 ? demo("events_2024") : demoOnly(`no ${year} schedule`)
        : apiGet("/api/v1/seasons/{year}/events", { path: { year } }),

    session: (key: string): Promise<SessionStatus> =>
      demoMode
        ? isDemoSession(key) ? demo("session_2024-05-Q")
          : Promise.resolve({ session_key: key, status: "not_ingested", info: null, manifest: null })
        : apiGet("/api/v1/sessions/{session_key}", { path: { session_key: key } }),

    ingest: (key: string, force = false): Promise<IngestResponse> =>
      demoMode
        ? demoOnly("ingestion needs the backend")
        : apiPost("/api/v1/sessions/{session_key}/ingest", { path: { session_key: key }, query: { force } }),

    drivers: (key: string): Promise<DriversResponse> =>
      demoMode ? isDemoSession(key) ? demo("drivers") : demoOnly(key)
        : apiGet("/api/v1/sessions/{session_key}/drivers", { path: { session_key: key } }),

    laps: (key: string, driver?: string): Promise<LapsResponse> =>
      demoMode
        ? isDemoSession(key)
          ? demo<LapsResponse>("laps").then((r) => driver
            ? { ...r, driver, laps: r.laps.filter((l) => l.driver === driver.toUpperCase() || l.driver_number === driver) }
            : r)
          : demoOnly(key)
        : apiGet("/api/v1/sessions/{session_key}/laps", { path: { session_key: key }, query: { driver } }),

    fastestLaps: (key: string): Promise<FastestLapsResponse> =>
      demoMode ? isDemoSession(key) ? demo("fastest_laps") : demoOnly(key)
        : apiGet("/api/v1/sessions/{session_key}/fastest-laps", { path: { session_key: key } }),

    corners: (key: string): Promise<CornersResponse> =>
      demoMode ? isDemoSession(key) ? demo("corners") : demoOnly(key)
        : apiGet("/api/v1/sessions/{session_key}/corners", { path: { session_key: key } }),

    stints: (key: string): Promise<StintsResponse> =>
      demoMode ? demoOnly("tyre analysis needs the backend")
        : apiGet("/api/v1/sessions/{session_key}/stints", { path: { session_key: key } }),

    tirePredict: (session: string, driver: string, lap: number, horizon = 25): Promise<TirePrediction> =>
      demoMode ? demoOnly("tyre forecasts need the backend")
        : apiGet("/api/v1/tires/predict", { query: { session, driver, lap, horizon } }),

    compare: (body: ComparisonRequest): Promise<ComparisonResponse> => {
      if (!demoMode) return apiPost("/api/v1/comparisons", { body });
      const isDemoPair = isDemoSession(body.session_key)
        && body.reference.driver.toUpperCase() === "VER" && lapLabel(body.reference.lap) === "fastest"
        && body.target.driver.toUpperCase() === "ALO" && lapLabel(body.target.lap) === "fastest";
      return isDemoPair ? demo("comparison_VER_ALO") : demoOnly("pick VER (fastest) vs ALO (fastest)");
    },
  };
}

export type Api = ReturnType<typeof createApi>;
