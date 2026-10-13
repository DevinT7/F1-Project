/** MSW server for component tests. Handlers serve real API responses from src/fixtures/api. */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import comparison from "@/fixtures/api/comparison_VER_ALO.json";
import corners from "@/fixtures/api/corners.json";
import drivers from "@/fixtures/api/drivers.json";
import events2024 from "@/fixtures/api/events_2024.json";
import fastestLaps from "@/fixtures/api/fastest_laps.json";
import laps from "@/fixtures/api/laps.json";
import seasons from "@/fixtures/api/seasons.json";
import session from "@/fixtures/api/session_2024-05-Q.json";
import raceDrivers from "@/fixtures/api/race_drivers.json";
import raceLaps from "@/fixtures/api/race_laps.json";
import raceSession from "@/fixtures/api/session_2024-99-R.json";
import raceStints from "@/fixtures/api/race_stints.json";
import qualiStints from "@/fixtures/api/stints_2024-05-Q.json";

/** Real /tires/predict responses (scripts/export_frontend_fixtures.py), keyed "AAA_40". */
const tireFixtures = Object.fromEntries(
  Object.entries(import.meta.glob("@/fixtures/api/tires_*.json", { eager: true, import: "default" }))
    .map(([path, body]) => [path.replace(/^.*tires_(.*)\.json$/, "$1"), body]),
) as Record<string, unknown>;

export const RACE = "2024-99-R";
const isRace = (key: unknown) => String(key).replace(/_/g, "-").toUpperCase() === RACE;

const V1 = "*/api/v1";

export const handlers = [
  http.get(`${V1}/seasons`, () => HttpResponse.json(seasons)),
  http.get(`${V1}/seasons/:year/events`, ({ params }) =>
    params.year === "2024" ? HttpResponse.json(events2024) : HttpResponse.json({ year: Number(params.year), events: [] })),
  http.get(`${V1}/sessions/:key`, ({ params }) =>
    String(params.key).replace(/_/g, "-") === "2024-05-Q"
      ? HttpResponse.json(session)
      : isRace(params.key) ? HttpResponse.json(raceSession)
        : HttpResponse.json({ session_key: params.key, status: "not_ingested", info: null, manifest: null })),
  http.get(`${V1}/sessions/:key/drivers`, ({ params }) => HttpResponse.json(isRace(params.key) ? raceDrivers : drivers)),
  http.get(`${V1}/sessions/:key/laps`, ({ request, params }) => {
    const source = isRace(params.key) ? raceLaps : laps;
    const driver = new URL(request.url).searchParams.get("driver");
    return HttpResponse.json(driver ? { ...source, driver, laps: source.laps.filter((l) => l.driver === driver) } : source);
  }),
  http.get(`${V1}/sessions/:key/stints`, ({ params }) => HttpResponse.json(isRace(params.key) ? raceStints : qualiStints)),
  http.get(`${V1}/tires/predict`, ({ request }) => {
    const q = new URL(request.url).searchParams;
    if (!isRace(q.get("session"))) {
      const body = tireFixtures["error_not_race"] as { error: { request_id: string } };
      return HttpResponse.json(body, { status: 422, headers: { "X-Request-ID": body.error.request_id } });
    }
    const body = tireFixtures[`${q.get("driver")}_${q.get("lap")}`];
    return body ? HttpResponse.json(body)
      : apiError(404, "lap_not_found", `no fixture for ${q.get("driver")} lap ${q.get("lap")}`);
  }),
  http.get(`${V1}/sessions/:key/fastest-laps`, () => HttpResponse.json(fastestLaps)),
  http.get(`${V1}/sessions/:key/corners`, () => HttpResponse.json(corners)),
  http.post(`${V1}/comparisons`, () => HttpResponse.json(comparison)),
];

export const server = setupServer(...handlers);

export function apiError(status: number, code: string, message: string, requestId = "req-test-123") {
  return HttpResponse.json(
    { error: { code, message, status_code: status, request_id: requestId } },
    { status, headers: { "X-Request-ID": requestId } },
  );
}
