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

const V1 = "*/api/v1";

export const handlers = [
  http.get(`${V1}/seasons`, () => HttpResponse.json(seasons)),
  http.get(`${V1}/seasons/:year/events`, ({ params }) =>
    params.year === "2024" ? HttpResponse.json(events2024) : HttpResponse.json({ year: Number(params.year), events: [] })),
  http.get(`${V1}/sessions/:key`, ({ params }) =>
    String(params.key).replace(/_/g, "-") === "2024-05-Q"
      ? HttpResponse.json(session)
      : HttpResponse.json({ session_key: params.key, status: "not_ingested", info: null, manifest: null })),
  http.get(`${V1}/sessions/:key/drivers`, () => HttpResponse.json(drivers)),
  http.get(`${V1}/sessions/:key/laps`, ({ request }) => {
    const driver = new URL(request.url).searchParams.get("driver");
    return HttpResponse.json(driver ? { ...laps, driver, laps: laps.laps.filter((l) => l.driver === driver) } : laps);
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
