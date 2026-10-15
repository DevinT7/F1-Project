import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { apiError, server } from "@/test/msw";

import { ApiError, apiGet, apiPost, buildUrl } from "./client";
import { isReportable } from "../telemetry";
import { createApi } from "./endpoints";

describe("buildUrl", () => {
  it("fills path params, encodes, and drops empty query values", () => {
    expect(buildUrl("/api/v1/sessions/{session_key}/laps", { session_key: "2024-05-Q" }, { driver: "VER", x: undefined, y: "" }))
      .toBe("http://localhost:3000/api/v1/sessions/2024-05-Q/laps?driver=VER");
    expect(() => buildUrl("/api/v1/sessions/{session_key}")).toThrow(/session_key/);
  });
});

describe("typed client", () => {
  it("returns the typed body", async () => {
    const laps = await apiGet("/api/v1/sessions/{session_key}/laps", { path: { session_key: "2024-05-Q" }, query: { driver: "VER" } });
    expect(laps.laps.every((l) => l.driver === "VER")).toBe(true);
    expect(laps.laps[0]?.lap_time_s === null || typeof laps.laps[0]?.lap_time_s === "number").toBe(true);
  });

  it("turns the error envelope into an ApiError with code and request id", async () => {
    server.use(http.post("*/api/v1/comparisons", () =>
      apiError(422, "lap_invalid", "ALO lap 1 is not valid for comparison (no_lap_time).", "rid-42")));
    const err = await apiPost("/api/v1/comparisons", {
      body: { session_key: "2024-05-Q", reference: { driver: "VER" }, target: { driver: "ALO", lap: 1 } },
    }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 422, code: "lap_invalid", requestId: "rid-42" });
    expect((err as ApiError).message).toContain("no_lap_time");
  });

  it("falls back to the header request id and status code when the body is not the envelope", async () => {
    server.use(http.get("*/api/v1/seasons", () =>
      new HttpResponse("upstream exploded", { status: 502, headers: { "X-Request-ID": "hdr-1" } })));
    await expect(apiGet("/api/v1/seasons")).rejects.toMatchObject({ status: 502, code: "http_502", requestId: "hdr-1" });
  });

  it("sends a fresh X-Request-ID the backend accepts on every call", async () => {
    const seen: (string | null)[] = [];
    server.use(http.get("*/api/v1/seasons", ({ request }) => {
      seen.push(request.headers.get("x-request-id"));
      return HttpResponse.json({ seasons: [] });
    }));
    await apiGet("/api/v1/seasons");
    await apiGet("/api/v1/seasons");
    expect(seen).toHaveLength(2);
    for (const id of seen) expect(id).toMatch(/^[A-Za-z0-9._-]{1,64}$/);
    expect(seen[0]).not.toBe(seen[1]);
  });

  it("only network failures, 5xx and crashes are reportable to Sentry", () => {
    expect(isReportable(new ApiError(0, "network_error", "x"))).toBe(true);
    expect(isReportable(new ApiError(503, "provider_error", "x"))).toBe(true);
    expect(isReportable(new ApiError(422, "lap_invalid", "x"))).toBe(false);
    expect(isReportable(new ApiError(401, "unauthorized", "x"))).toBe(false);
    expect(isReportable(new TypeError("render crash"))).toBe(true);
  });

  it("reports network failures with a friendly code", async () => {
    server.use(http.get("*/api/v1/seasons", () => HttpResponse.error()));
    await expect(apiGet("/api/v1/seasons")).rejects.toMatchObject({ status: 0, code: "network_error" });
  });
});

describe("demo mode", () => {
  const demo = createApi(true);

  it("serves real exported responses with no network", async () => {
    server.use(http.all("*", () => { throw new Error("demo mode must not hit the network"); }));
    const cmp = await demo.compare({ session_key: "2024-05-Q", reference: { driver: "VER", lap: "fastest" }, target: { driver: "ALO", lap: "fastest" } });
    expect(cmp.meta.reference.driver).toBe("VER");
    expect(cmp.delta_s.length).toBe(cmp.meta.points);
    expect((await demo.laps("2024-05-Q", "ALO")).laps.every((l) => l.driver === "ALO")).toBe(true);
    expect((await demo.session("2024-06-Q")).status).toBe("not_ingested");
  });

  it("explains what it cannot serve", async () => {
    await expect(demo.compare({ session_key: "2024-05-Q", reference: { driver: "PER" }, target: { driver: "ALO" } }))
      .rejects.toMatchObject({ code: "demo_unavailable" });
  });
});
