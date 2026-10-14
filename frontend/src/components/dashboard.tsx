"use client";

import { useEffect, useMemo } from "react";

import { DEMO_REFERENCE, DEMO_SESSION, DEMO_TARGET } from "@/lib/api/endpoints";
import { ApiError } from "@/lib/api/client";
import { features } from "@/lib/flags";
import {
  comparisonRequest, useComparison, useCorners, useDrivers, useFastestLaps, useLaps, useSessionStatus,
} from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import { isComparable, parseSessionKey, serializeUrlState, type UrlState } from "@/lib/url-state";
import { useUrlState } from "@/lib/use-url-state";

import { AccountMenu, SignInDialog } from "./account";
import { ComparisonHeader } from "./comparison-header";
import { EngineerSection } from "./engineer";
import { ErrorBoundary } from "./error-boundary";
import { CornerPanel, FindingsList, SectorCard } from "./findings";
import { LapPicker } from "./lap-picker";
import { SessionSelector } from "./session-selector";
import { PlaceholderCard, Sidebar, Toaster, Topbar } from "./shell";
import { TelemetryStack } from "./telemetry-stack";
import { TireSection } from "./tire-section";
import { TrackMap } from "./track-map";

export function Dashboard() {
  const [state, update] = useUrlState();
  const status = useSessionStatus(state.session);
  const ready = status.data?.status === "ready";
  const drivers = useDrivers(state.session, ready);
  const laps = useLaps(state.session, ready);
  const fastest = useFastestLaps(state.session, ready);
  const corners = useCorners(state.session, ready);
  const selectCorner = useUiStore((s) => s.selectCorner);

  // A fresh session with no laps chosen: preselect the two fastest drivers' fastest laps.
  useEffect(() => {
    if (!ready || state.ref || state.tgt || !fastest.data) return;
    const top = fastest.data.laps.filter((l) => l.lap).slice(0, 2);
    if (top.length === 2) {
      update({ ref: { driver: top[0]!.driver, lap: "fastest" }, tgt: { driver: top[1]!.driver, lap: "fastest" } });
    }
  }, [ready, state.ref, state.tgt, fastest.data, update]);

  const request = useMemo(
    () => (isComparable(state) && ready ? comparisonRequest(state.session, state.ref, state.tgt, state.resolution) : null),
    [state, ready],
  );
  const comparison = useComparison(request);

  // a new comparison starts with no corner pinned (CornerPanel then shows the biggest loss)
  useEffect(() => selectCorner(null), [comparison.data, selectCorner]);

  const parts = parseSessionKey(state.session);
  const demoState: UrlState = { session: DEMO_SESSION, ref: { ...DEMO_REFERENCE }, tgt: { ...DEMO_TARGET }, resolution: 2, demo: true };
  const sessionLabel = status.data?.info ? `${status.data.info.year} · ${status.data.info.event_name} · ${status.data.info.session_code}` : state.session;

  return (
    <div className="lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
      <Sidebar sessionLabel={sessionLabel} />
      <main className="mx-auto w-full max-w-[1500px] px-4 pt-6 pb-16 sm:px-8">
        <Topbar demo={state.demo} season={parts?.year ?? null} account={<AccountMenu demo={state.demo} current={state} />}
          demoHref={`?${serializeUrlState(demoState)}`}
          liveHref={`?${serializeUrlState({ ...state, demo: false })}`} />
        {state.demo && (
          <p className="mb-3 rounded-xl border border-line bg-purple-soft px-4 py-2 text-sm text-purple" role="status">
            ▶ Demo data: real API output for 2024 China qualifying (VER vs ALO), served without a backend.
          </p>
        )}

        <div className="flex flex-col gap-3">
          <SessionSelector value={state.session} demo={state.demo}
            onChange={(session) => update({ session, ref: null, tgt: null, tyre: null, asof: null })} />
          {ready && (
            <div className="strip" role="group" aria-label="Laps to compare">
              <LapPicker role="reference" value={state.ref} drivers={drivers.data?.drivers ?? []}
                laps={laps.data?.laps ?? []} loading={drivers.isPending || laps.isPending}
                onChange={(ref) => update({ ref })} />
              <LapPicker role="target" value={state.tgt} drivers={drivers.data?.drivers ?? []}
                laps={laps.data?.laps ?? []} loading={drivers.isPending || laps.isPending}
                onChange={(tgt) => update({ tgt })} />
            </div>
          )}
        </div>

        <div className="mt-5">
          {!state.session && (
            <EmptyState>
              Pick a season, Grand Prix and session to start, or try <b>Demo data</b> to preview with real telemetry and no backend.
            </EmptyState>
          )}
          {state.session && ready && !isComparable(state) && <EmptyState>Choose a reference and a target lap.</EmptyState>}
          {request && comparison.isPending && <ComparisonSkeleton />}
          {request && comparison.isError && <ComparisonError error={comparison.error} onRetry={() => comparison.refetch()} />}
          {comparison.data && request && (
            <div className="flex flex-col gap-5">
              <ErrorBoundary label="Comparison summary"><ComparisonHeader comparison={comparison.data} /></ErrorBoundary>
              <div className="grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
                <ErrorBoundary label="Telemetry"><TelemetryStack comparison={comparison.data} /></ErrorBoundary>
                <section id="track" className="card min-w-0" aria-labelledby="track-heading">
                  <div className="kick">Interactive track map</div>
                  <h2 id="track-heading" className="mb-2 text-[15px] font-bold">{status.data?.info?.circuit ?? "Circuit"} · time gained / lost by corner</h2>
                  <div className="flex flex-col gap-4 2xl:flex-row">
                    <div className="min-w-0 flex-1">
                      <ErrorBoundary label="Track map"><TrackMap comparison={comparison.data} corners={corners.data} /></ErrorBoundary>
                    </div>
                    <div className="2xl:w-[300px]"><CornerPanel comparison={comparison.data} /></div>
                  </div>
                </section>
              </div>
              <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-4">
                <div className="md:col-span-2"><FindingsList comparison={comparison.data} /></div>
                <SectorCard comparison={comparison.data} />
                {!features.engineer && (
                  <div className="flex flex-col gap-5">
                    <PlaceholderCard id="engineer" kick="✦ AI engineer" title="Race engineer report" enabled={false} />
                  </div>
                )}
              </div>
              {features.engineer && state.session && state.ref && state.tgt && (
                <ErrorBoundary label="AI engineer">
                  <EngineerSection session={state.session} reference={state.ref} target={state.tgt}
                    sessionCode={status.data?.info?.session_code ?? null} />
                </ErrorBoundary>
              )}
            </div>
          )}
        </div>
        {ready && features.tires && status.data?.info && state.session && (
          <div className="mt-5">
            <ErrorBoundary label="Tire analysis">
              <TireSection session={state.session} sessionCode={status.data.info.session_code}
                raceLaps={status.data.info.n_laps_scheduled ?? null} drivers={drivers.data?.drivers ?? []}
                laps={laps.data?.laps ?? []} tyre={state.tyre ?? null} asof={state.asof ?? null}
                onChange={update} />
            </ErrorBoundary>
          </div>
        )}
        <p className="mt-8 text-center text-[11px] text-muted">Data: FastF1 · every figure is computed by the RaceIQ backend</p>
      </main>
      <Toaster />
      <SignInDialog />
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="card text-sm text-muted" role="status">{children}</div>;
}

function ComparisonSkeleton() {
  return (
    <div className="flex flex-col gap-5" role="status" aria-label="Loading comparison">
      <div className="skeleton h-[104px]" />
      <div className="grid gap-5 xl:grid-cols-[1.45fr_1fr]">
        <div className="skeleton h-[700px]" />
        <div className="skeleton h-[520px]" />
      </div>
      <span className="sr-only">Aligning laps and computing findings…</span>
    </div>
  );
}

function ComparisonError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="card border-red bg-red-soft" role="alert">
      <div className="font-bold text-red-ink">⚠ Couldn&apos;t compare these laps</div>
      <p className="mt-1 text-sm text-ink-2">{error.message}</p>
      {api && (
        <p className="mt-2 flex flex-wrap gap-1.5 text-xs">
          <span className="chip">code: {api.code}</span>
          {api.requestId && <span className="chip">request_id: {api.requestId}</span>}
        </p>
      )}
      <button type="button" className="btn btn-ghost mt-3" onClick={onRetry}>Retry</button>
    </div>
  );
}
