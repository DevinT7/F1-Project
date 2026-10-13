"use client";
/**
 * The dashboard's tyre analysis section. Race sessions: driver → StintTimeline → TirePanel with
 * the "as of" slider. Other sessions: the non-race notice with raw stint data, plus the
 * qualifying lap-time-vs-tyre-age chart for qualifying sessions.
 */
import { useEffect, useMemo } from "react";

import type { DriverInfo, LapRecord } from "@/lib/api/endpoints";
import { useStints, useTirePrediction } from "@/lib/queries";
import { asOfBounds, defaultAsOf, stintForLap, tireViewState, type StintSummary } from "@/lib/tires";

import { QualifyingTireChart } from "./qualifying-tire-chart";
import { StintTimeline } from "./stint-timeline";
import { NonRaceNotice, TirePanel } from "./tire-panel";

const CLEAN_LAPS_REQUIRED = 5; // the API's default; every response restates it (clean_laps_required)

export interface TireSectionProps {
  session: string;
  sessionCode: string;
  raceLaps: number | null;
  drivers: DriverInfo[];
  laps: LapRecord[];
  tyre: string | null;
  asof: number | null;
  onChange: (next: { tyre?: string | null; asof?: number | null }) => void;
}

export function TireSection({ session, sessionCode, raceLaps, drivers, laps, tyre, asof, onChange }: TireSectionProps) {
  const isRace = sessionCode === "R";
  const stints = useStints(session, true);
  const driver = tyre ?? drivers[0]?.code ?? null;
  const driverStints: StintSummary[] = useMemo(
    () => stints.data?.drivers.find((d) => d.driver === driver)?.stints ?? [],
    [stints.data, driver]);
  const lastLap = raceLaps ?? Math.max(0, ...driverStints.map((s) => s.last_lap));

  // a race with no "as of" yet: start at the first stint that can be forecast
  useEffect(() => {
    if (!isRace || asof !== null || !driver || !driverStints.length) return;
    const first = driverStints.find((s) => s.model_eligible) ?? driverStints[0]!;
    onChange({ tyre: driver, asof: defaultAsOf(first, raceLaps) });
  }, [isRace, asof, driver, driverStints, raceLaps, onChange]);

  const stint = asof !== null ? stintForLap(driverStints, asof) : null;
  // the chart wants the whole window horizon (25 laps), but never more laps than the race has left
  const horizon = asof !== null && raceLaps ? Math.max(1, Math.min(25, raceLaps - asof)) : 25;
  const prediction = useTirePrediction(session, driver, asof, horizon, isRace);
  const view = tireViewState({ isPending: prediction.isPending, data: prediction.data, error: prediction.error });
  const bounds = stint ? asOfBounds(stint, CLEAN_LAPS_REQUIRED, raceLaps) : null;

  return (
    <section id="tires" className="card" aria-labelledby="tires-title" data-testid="tire-section">
      <div className="kick">Tire analysis</div>
      <h2 id="tires-title" className="mt-1 text-[15px] font-bold">
        {isRace ? "Degradation, forecast & pit window" : "Stints and tyre usage"}
      </h2>

      <div className="mt-3 grid gap-3 sm:grid-cols-[220px_minmax(0,1fr)] sm:items-end">
        <div>
          <label className="field-label" htmlFor="tyre-driver">Driver</label>
          <select id="tyre-driver" className="field-select" value={driver ?? ""}
            onChange={(e) => onChange({ tyre: e.target.value || null, asof: null })}>
            {drivers.map((d) => <option key={d.code} value={d.code}>{d.code}{d.full_name ? ` · ${d.full_name}` : ""}</option>)}
          </select>
        </div>
        {driver && driverStints.length > 0 && (
          <StintTimeline driver={driver} stints={driverStints} raceLaps={lastLap} selectedStint={stint?.stint ?? null}
            onSelect={(s) => onChange({ tyre: driver, asof: defaultAsOf(s, raceLaps) })} />
        )}
        {stints.isPending && <div className="skeleton h-11" aria-label="Loading stints" />}
      </div>

      {!isRace && (
        <>
          <NonRaceNotice />
          {sessionCode === "Q" && driver && <div className="mt-4"><QualifyingTireChart laps={laps} driver={driver} /></div>}
        </>
      )}
      {isRace && asof !== null && (
        <div className="mt-4">
          <TirePanel view={view} asOf={{ value: asof, bounds, onChange: (lap) => onChange({ tyre: driver, asof: lap }) }} />
        </div>
      )}
    </section>
  );
}
