"use client";
/**
 * TanStack Query hooks over the typed API. Historical F1 data never changes once ingested, so
 * session data and comparisons are cached for the life of the page.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext } from "react";

import type { Api, ComparisonRequest, EngineerAskRequest, EngineerReportRequest } from "./api/endpoints";
import { HANDLED_CODES } from "./tires";
import type { LapRef } from "./url-state";

export const ApiContext = createContext<{ api: Api; demo: boolean } | null>(null);

export function useApi(): { api: Api; demo: boolean } {
  const ctx = useContext(ApiContext);
  if (!ctx) throw new Error("useApi must be used inside <Providers>");
  return ctx;
}

const forever = { staleTime: Infinity, gcTime: 30 * 60_000 } as const;

export const keys = {
  seasons: (demo: boolean) => ["seasons", demo] as const,
  events: (demo: boolean, year: number) => ["events", demo, year] as const,
  session: (demo: boolean, key: string) => ["session", demo, key] as const,
  sessionData: (demo: boolean, key: string, what: string) => ["session", demo, key, what] as const,
  comparison: (demo: boolean, req: ComparisonRequest) => ["comparison", demo, req] as const,
};

export function useSeasons() {
  const { api, demo } = useApi();
  return useQuery({ queryKey: keys.seasons(demo), queryFn: () => api.seasons(), ...forever });
}

export function useEvents(year: number | null) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: keys.events(demo, year ?? 0),
    queryFn: () => api.events(year!),
    enabled: year !== null,
    staleTime: 60 * 60_000,
  });
}

export function useSessionStatus(key: string | null) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: keys.session(demo, key ?? ""),
    queryFn: () => api.session(key!),
    enabled: key !== null,
    staleTime: 60_000,
  });
}

/** Start an ingest job (POST → 202); the job is then polled with useJob. */
export function useIngest(key: string | null) {
  const { api, demo } = useApi();
  return useMutation({
    mutationKey: ["ingest", demo, key],
    mutationFn: () => api.ingest(key!),
  });
}

const JOB_DONE = new Set(["succeeded", "failed"]);
export const JOB_POLL_MS = 1500;

/** Poll a background job until it succeeds or fails; on success the session status is refreshed. */
export function useJob(id: string | null, sessionKey: string | null) {
  const { api, demo } = useApi();
  const client = useQueryClient();
  return useQuery({
    queryKey: ["job", demo, id],
    queryFn: async () => {
      const job = await api.job(id!);
      if (job.status === "succeeded" && sessionKey) {
        await client.invalidateQueries({ queryKey: keys.session(demo, sessionKey) });
      }
      return job;
    },
    enabled: id !== null,
    refetchInterval: (query) => (query.state.data && JOB_DONE.has(query.state.data.status) ? false : JOB_POLL_MS),
    retry: 2,
  });
}

function useSessionData<T>(key: string | null, ready: boolean, what: string, fn: (api: Api, key: string) => Promise<T>) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: keys.sessionData(demo, key ?? "", what),
    queryFn: () => fn(api, key!),
    enabled: key !== null && ready,
    ...forever,
  });
}

export const useDrivers = (key: string | null, ready: boolean) =>
  useSessionData(key, ready, "drivers", (api, k) => api.drivers(k));
export const useLaps = (key: string | null, ready: boolean) =>
  useSessionData(key, ready, "laps", (api, k) => api.laps(k));
export const useFastestLaps = (key: string | null, ready: boolean) =>
  useSessionData(key, ready, "fastest", (api, k) => api.fastestLaps(k));
export const useCorners = (key: string | null, ready: boolean) =>
  useSessionData(key, ready, "corners", (api, k) => api.corners(k));

export const useStints = (key: string | null, ready: boolean) =>
  useSessionData(key, ready, "stints", (api, k) => api.stints(k));

/** The tyre forecast "as of" a lap. Expected panel states (503, non-race) are not toasted. */
export function useTirePrediction(session: string | null, driver: string | null, lap: number | null, horizon: number,
  enabled = true) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: ["tires", demo, session, driver, lap, horizon] as const,
    queryFn: () => api.tirePredict(session!, driver!, lap!, horizon),
    enabled: enabled && session !== null && driver !== null && lap !== null,
    retry: false,
    meta: { handledCodes: HANDLED_CODES },
    ...forever,
  });
}

export function comparisonRequest(session: string, ref: LapRef, tgt: LapRef, resolution: number): ComparisonRequest {
  return { session_key: session, reference: { ...ref }, target: { ...tgt }, resolution_m: resolution };
}

export function useComparison(req: ComparisonRequest | null) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: req ? keys.comparison(demo, req) : ["comparison", demo, null],
    queryFn: () => api.compare(req!),
    enabled: req !== null,
    retry: false,
    ...forever,
  });
}

/** The engineer report (non-streaming: it arrives only after the server validated it). */
export function useEngineerReport(req: EngineerReportRequest | null) {
  const { api, demo } = useApi();
  return useQuery({
    queryKey: ["engineer-report", demo, req] as const,
    queryFn: () => api.engineerReport(req!),
    enabled: req !== null,
    retry: false,
    ...forever,
  });
}

/** Regenerate: bypasses the server cache and replaces the cached report. */
export function useRegenerateReport(req: EngineerReportRequest | null) {
  const { api, demo } = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationKey: ["engineer-regenerate", demo, req],
    mutationFn: () => api.engineerReport({ ...req!, regenerate: true }),
    onSuccess: (data) => client.setQueryData(["engineer-report", demo, req], data),
  });
}

/** One question to the engineer. Errors are shown inline by the chat (meta.inlineErrors: no toast). */
export function useAskEngineer() {
  const { api, demo } = useApi();
  return useMutation({
    mutationKey: ["engineer-ask", demo],
    mutationFn: (body: EngineerAskRequest) => api.engineerAsk(body),
    meta: { inlineErrors: true },
  });
}
