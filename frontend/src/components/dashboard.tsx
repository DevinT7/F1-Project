"use client";

import { useCallback, useEffect, useMemo, type ReactNode } from "react";

import { DEMO_REFERENCE, DEMO_SESSION, DEMO_TARGET, type ComparisonResponse, type CornersResponse } from "@/lib/api/endpoints";
import { ApiError } from "@/lib/api/client";
import { features } from "@/lib/flags";
import {
  comparisonRequest, useComparison, useCorners, useDrivers, useFastestLaps, useLaps, useSessionStatus,
} from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import { TabNavContext } from "@/lib/tab-nav";
import { DEFAULT_TAB, tabLabel, type TabId } from "@/lib/tabs";
import { isComparable, parseSessionKey, serializeUrlState, type UrlState } from "@/lib/url-state";
import { useUrlState } from "@/lib/use-url-state";

import { CornerTable, RaceBrief, StraightsTable } from "./briefing";
import { ComparisonHeader } from "./comparison-header";
import { EngineerSection } from "./engineer";
import { ErrorBoundary } from "./error-boundary";
import { CornerPanel, FindingsList, SectorCard } from "./findings";
import { LapPicker } from "./lap-picker";
import { SessionSelector } from "./session-selector";
import { PlaceholderCard, Toaster, TopNav } from "./shell";
import { TabStage } from "./tab-stage";
import { TelemetryStack } from "./telemetry-stack";
import { TireSection } from "./tire-section";
import { TrackMap, type CornerLabelMode } from "./track-map";

const TAB_INTRO: Record<TabId, string> = {
  pitwall: "The whole comparison at a glance: lap times, the brief, the map and the corners that decided it.",
  laps: "Speed, throttle, brake, gear and the running time delta, metre by metre, with the speed traps.",
  track: "Where on the circuit the time went, corner by corner, with a real-pace replay of both laps.",
  tires: "Stints, degradation and the pit window for race sessions; observed laps for the others.",
  engineer: "The AI race engineer: its full report, written only from verified numbers, the facts behind it, and questions.",
};

export function Dashboard() {
  const [state, update] = useUrlState();
  const tab = state.tab ?? DEFAULT_TAB;
  const status = useSessionStatus(state.session);
  const ready = status.data?.status === "ready";
  const drivers = useDrivers(state.session, ready);
  const laps = useLaps(state.session, ready);
  const fastest = useFastestLaps(state.session, ready);
  const corners = useCorners(state.session, ready);
  const selectCorner = useUiStore((s) => s.selectCorner);
  const goTo = useCallback((t: TabId) => update({ tab: t }), [update]);

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
  const demoState: UrlState = { session: DEMO_SESSION, ref: { ...DEMO_REFERENCE }, tgt: { ...DEMO_TARGET }, resolution: 2, demo: true, tab };
  const info = status.data?.info;
  const sessionLabel = info ? `${info.year} · ${info.event_name} · ${info.session_code}` : state.session;
  const sessionCode = info?.session_code ?? null;

  function comparisonGate(render: (cmp: ComparisonResponse) => ReactNode): ReactNode {
    if (!state.session) {
      return <EmptyState>Pick a season, Grand Prix and session to start, or try <b>Demo data</b> to preview with real telemetry and no backend.</EmptyState>;
    }
    if (ready && !isComparable(state)) return <EmptyState>Choose a reference and a target lap.</EmptyState>;
    if (request && comparison.isPending) return <ComparisonSkeleton />;
    if (request && comparison.isError) return <ComparisonError error={comparison.error} onRetry={() => comparison.refetch()} />;
    if (comparison.data && request) return render(comparison.data);
    return null;
  }

  function panel(): ReactNode {
    switch (tab) {
      case "pitwall":
        return comparisonGate((cmp) => <PitWall comparison={cmp} corners={corners.data} />);
      case "laps":
        return comparisonGate((cmp) => <LapComparison comparison={cmp} corners={corners.data} />);
      case "track":
        return comparisonGate((cmp) => <TrackTab comparison={cmp} corners={corners.data} />);
      case "tires":
        if (!state.session) return <EmptyState>Pick a race session to see stints, degradation and the pit window.</EmptyState>;
        if (!(ready && features.tires && info)) return null;
        return (
          <ErrorBoundary label="Tire analysis">
            <TireSection session={state.session} sessionCode={info.session_code}
              raceLaps={info.n_laps_scheduled ?? null} drivers={drivers.data?.drivers ?? []}
              laps={laps.data?.laps ?? []} tyre={state.tyre ?? null} asof={state.asof ?? null}
              onChange={update} />
          </ErrorBoundary>
        );
      case "engineer":
        return comparisonGate(() => (features.engineer && state.session && state.ref && state.tgt ? (
          <ErrorBoundary label="AI engineer">
            <EngineerSection session={state.session} reference={state.ref} target={state.tgt} sessionCode={sessionCode} />
          </ErrorBoundary>
        ) : <PlaceholderCard id="engineer" kick="✦ AI engineer" title="Race engineer report" enabled={false} />));
    }
  }

  return (
    <TabNavContext.Provider value={goTo}>
      <div className="min-h-screen">
        <TopNav tab={tab} onTab={goTo} demo={state.demo}
          demoHref={`?${serializeUrlState(demoState)}`}
          liveHref={`?${serializeUrlState({ ...state, demo: false })}`} />
        <div className="mx-auto max-w-[1920px]">
          <main className="min-w-0 px-4 pt-6 pb-16 sm:px-8">
            <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
              <div className="min-w-0">
                <div className="eyebrow">◣ {sessionLabel ?? "Race analysis"}{parts ? ` · ${parts.year} season` : ""}</div>
                <h1 key={tab} className="display tab-enter mt-1 text-[40px] sm:text-[48px]">{tabLabel(tab)}</h1>
                <p className="mt-1 max-w-3xl text-[15px] text-muted">{TAB_INTRO[tab]}</p>
              </div>
            </header>
            {state.demo && (
              <p className="mb-3 rounded-lg border border-line bg-purple-soft px-4 py-2 text-sm text-purple" role="status">
                ▶ Demo data: real API output for 2024 China qualifying (VER vs ALO), served without a backend.
              </p>
            )}

            <div className="flex flex-col gap-3">
              <SessionSelector value={state.session} demo={state.demo}
                onChange={(session) => update({ session, ref: null, tgt: null, tyre: null, asof: null })} />
              {ready && tab !== "tires" && (
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
              <TabStage tab={tab}>{panel()}</TabStage>
            </div>

            <footer className="mt-10 text-center text-[11px] leading-relaxed text-muted" data-testid="site-notice">
              <p>Data: FastF1 · every figure is computed by the Senna analysis engine</p>
              <p>
                Senna is an unofficial, non-commercial project, not associated in any way with the Formula 1 companies.
                F1, FORMULA ONE, FORMULA 1 and GRAND PRIX are trade marks of Formula One Licensing B.V.
                Timing data via the open-source FastF1 library.
              </p>
            </footer>
          </main>
        </div>
        <Toaster />
      </div>
    </TabNavContext.Provider>
  );
}

// ---------------------------------------------------------------- the tabs
function MapCard({ comparison, corners, maxHeight, title, labels = "leaders" }: {
  comparison: ComparisonResponse; corners: CornersResponse | undefined; maxHeight: number; title?: string;
  labels?: CornerLabelMode;
}) {
  return (
    <section id="track" className="card min-w-0" aria-labelledby="track-heading">
      <div className="kick">Interactive track map</div>
      <h2 id="track-heading" className="mb-2 text-[16px] font-bold">{title ?? `${comparison.findings.session_context?.event_name ?? "Circuit"} · time gained / lost by corner`}</h2>
      <ErrorBoundary label="Track map"><TrackMap comparison={comparison} corners={corners} maxHeight={maxHeight} labels={labels} /></ErrorBoundary>
    </section>
  );
}

function PitWall({ comparison, corners }: { comparison: ComparisonResponse; corners: CornersResponse | undefined }) {
  return (
    <div className="rise flex flex-col gap-5">
      <ErrorBoundary label="Comparison summary"><ComparisonHeader comparison={comparison} /></ErrorBoundary>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <MapCard comparison={comparison} corners={corners} maxHeight={760} />
        <CornerPanel comparison={comparison} />
      </div>
      <RaceBrief comparison={comparison} />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <FindingsList comparison={comparison} />
        <SectorCard comparison={comparison} />
      </div>
    </div>
  );
}

function LapComparison({ comparison, corners }: { comparison: ComparisonResponse; corners: CornersResponse | undefined }) {
  return (
    <div className="rise flex flex-col gap-5">
      <ErrorBoundary label="Comparison summary"><ComparisonHeader comparison={comparison} /></ErrorBoundary>
      <ErrorBoundary label="Telemetry"><TelemetryStack comparison={comparison} height={920} /></ErrorBoundary>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <StraightsTable comparison={comparison} />
        <MapCard comparison={comparison} corners={corners} maxHeight={320} title="Track layout" labels="none" />
      </div>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <FindingsList comparison={comparison} />
        <SectorCard comparison={comparison} />
      </div>
    </div>
  );
}

function TrackTab({ comparison, corners }: { comparison: ComparisonResponse; corners: CornersResponse | undefined }) {
  return (
    <div className="rise flex flex-col gap-5">
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <MapCard comparison={comparison} corners={corners} maxHeight={680} />
        <CornerPanel comparison={comparison} />
      </div>
      <CornerTable comparison={comparison} />
    </div>
  );
}

// ---------------------------------------------------------------- states
function EmptyState({ children }: { children: ReactNode }) {
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
