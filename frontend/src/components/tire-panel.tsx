"use client";
/**
 * Tyre panel for a race: what the model says about the selected stint "as of" lap N.
 * Every state the API can produce has its own rendering (lib/tires.ts tireViewState):
 * loading · ok (+ rain notice) · insufficient_data (progress "2/5 clean laps") · wet tyres ·
 * model unavailable (503) · non-race session · error (toast with request id, raised globally).
 */
import { useId, useState } from "react";

import { formatLapTime, MISSING } from "@/lib/format";
import {
  chartSummary, clampCompound, COMPOUNDS, DEGRADATION_TOOLTIP, formatDegradation, formatWindow,
  type TirePrediction, type TireView,
} from "@/lib/tires";

import { TireChart } from "./tire-chart";

export interface AsOfControl {
  value: number;
  bounds: [number, number] | null;
  onChange: (lap: number) => void;
}

export function TirePanel({ view, asOf }: { view: TireView; asOf: AsOfControl | null }) {
  switch (view.kind) {
    case "loading":
      return (
        <div data-testid="tire-state-loading" role="status" aria-label="Loading tyre forecast">
          <div className="skeleton h-[60px]" />
          <div className="skeleton mt-3 h-[340px]" />
          <span className="sr-only">Loading the tyre forecast…</span>
        </div>
      );
    case "model_unavailable":
      return (
        <Notice testId="tire-state-model-unavailable" tone="amber" title="Tyre model unavailable">
          <p>The prediction service has no usable tyre model right now (HTTP 503), so no forecast is shown. Stint data above is still accurate.</p>
          <p className="mt-1 text-xs text-muted">{view.message}</p>
          {view.requestId && <span className="chip mt-2">request_id: {view.requestId}</span>}
        </Notice>
      );
    case "non_race":
      return <NonRaceNotice />;
    case "error":
      return (
        <Notice testId="tire-state-error" tone="red" title="Couldn't load the tyre forecast">
          <p>{view.error.message}</p>
          <p className="mt-2 flex flex-wrap gap-1.5 text-xs">
            <span className="chip">code: {view.code}</span>
            {view.requestId && <span className="chip">request_id: {view.requestId}</span>}
          </p>
        </Notice>
      );
    case "wet":
      return (
        <div data-testid="tire-state-wet">
          <Header p={view.prediction} />
          <Notice testId="tire-wet-notice" tone="blue" title="Wet conditions">
            <p>{COMPOUNDS[clampCompound(view.prediction.compound)].label} tyres are not modelled: the tyre model is trained on dry races only. Observed laps are shown without a forecast.</p>
          </Notice>
          <Observed p={view.prediction} />
        </div>
      );
    case "insufficient":
      return (
        <div data-testid="tire-state-insufficient">
          <Header p={view.prediction} />
          {asOf && <AsOfSlider control={asOf} />}
          <Notice testId="tire-insufficient" tone="amber" title="Not enough clean laps yet">
            <p>A forecast needs at least {view.required} clean laps in the current stint (laps after a pit stop, safety cars,
              lap 1 and outliers do not count). {view.prediction.reason_code === "too_few_clean_laps" ? "" : view.prediction.reason}</p>
            <div className="mt-2 flex items-center gap-3">
              <span className="font-bold" data-testid="clean-lap-progress">{view.used}/{view.required} clean laps</span>
              <span className="h-2 flex-1 overflow-hidden rounded bg-line" aria-hidden>
                <i className="block h-full bg-amber-ink" style={{ width: `${Math.min(100, (view.used / view.required) * 100)}%` }} />
              </span>
            </div>
          </Notice>
          <Observed p={view.prediction} />
        </div>
      );
    case "ok":
      return <Forecast p={view.prediction} wet={view.wet} asOf={asOf} />;
  }
}

export function NonRaceNotice() {
  return (
    <Notice testId="tire-state-non-race" tone="blue" title="Tyre forecasting requires a race">
      <p>Tire forecasting requires a race; showing raw stint data.</p>
    </Notice>
  );
}

function Notice({ testId, tone, title, children }: { testId: string; tone: "amber" | "red" | "blue"; title: string; children: React.ReactNode }) {
  const styles = { amber: "bg-amber-soft text-amber-ink", red: "bg-red-soft text-red-ink", blue: "bg-blue-soft text-blue" }[tone];
  return (
    <div data-testid={testId} role={tone === "red" ? "alert" : "status"} className={`mt-3 rounded-xl border border-line p-3.5 text-sm ${styles}`}>
      <div className="font-bold">{tone === "red" ? "⚠ " : ""}{title}</div>
      <div className="mt-1 text-ink-2">{children}</div>
    </div>
  );
}

function Badge({ compound }: { compound: string | null | undefined }) {
  const c = COMPOUNDS[clampCompound(compound)];
  return (
    <span className="flex h-14 w-14 flex-none items-center justify-center rounded-full border-4 text-xl font-extrabold"
      style={{ borderColor: c.color === "#f0f0ec" ? "#bdb8b2" : c.color, color: "var(--ink)" }} aria-hidden>{c.letter}</span>
  );
}

function Header({ p }: { p: TirePrediction }) {
  const c = COMPOUNDS[clampCompound(p.compound)];
  return (
    <div className="flex items-center gap-3.5" data-testid="tire-header">
      <Badge compound={p.compound} />
      <div>
        <div className="text-[15px] font-bold">{c.label}{p.tyre_age != null ? ` · ${p.tyre_age} laps old` : ""}</div>
        <div className="text-xs text-muted">
          Stint {p.stint ?? MISSING} · as of lap {p.lap} · {p.clean_laps_used ?? 0} clean lap{p.clean_laps_used === 1 ? "" : "s"} used
        </div>
      </div>
    </div>
  );
}

function AsOfSlider({ control }: { control: AsOfControl }) {
  const id = useId();
  if (!control.bounds) return null;
  const [lo, hi] = control.bounds;
  return (
    <div className="mt-3 rounded-xl border border-line bg-card-2 p-3">
      <label htmlFor={id} className="flex items-baseline justify-between text-xs font-bold">
        <span>Forecast as of lap <span data-testid="as-of-value">{control.value}</span></span>
        <span className="font-normal text-muted">laps {lo}–{hi}</span>
      </label>
      <input id={id} type="range" min={lo} max={hi} step={1} value={Math.min(hi, Math.max(lo, control.value))}
        onChange={(e) => control.onChange(Number(e.target.value))} className="mt-2 w-full accent-[var(--accent)]"
        aria-describedby={`${id}-note`} data-testid="as-of-slider" />
      <p id={`${id}-note`} className="mt-1 text-[11px] text-muted">
        The forecast only uses laps up to {control.value}; later laps are never read. Only laps with enough clean data are offered.
      </p>
    </div>
  );
}

function Observed({ p }: { p: TirePrediction }) {
  const hist = p.history ?? [];
  if (!hist.length) return null;
  return (
    <details className="mt-3 text-xs" data-testid="observed-laps">
      <summary className="cursor-pointer font-bold">Observed laps in this stint ({hist.length})</summary>
      <ul className="mt-1 grid grid-cols-2 gap-x-4 sm:grid-cols-3">
        {hist.map((h) => (
          <li key={h.lap} className={h.clean ? "" : "text-muted"}>
            Lap {h.lap}: {formatLapTime(h.lap_time_s)}{h.clean ? "" : ` (excluded: ${(h.excluded_reason ?? "").replace(/_/g, " ")})`}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Forecast({ p, wet, asOf }: { p: TirePrediction; wet: boolean; asOf: AsOfControl | null }) {
  const [showExcluded, setShowExcluded] = useState(true);
  const summaryId = useId();
  const w = p.pit_window;
  return (
    <div data-testid="tire-state-ok">
      <Header p={p} />
      {wet && (
        <Notice testId="tire-wet-notice" tone="blue" title="Wet conditions">
          <p>Rain was reported during this stint. The model is trained on dry races only, so treat this forecast with extra caution.</p>
        </Notice>
      )}
      {asOf && <AsOfSlider control={asOf} />}

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-line p-3" data-testid="degradation-card">
          <div className="kick flex items-center gap-1.5">Degradation
            <button type="button" className="text-muted" aria-label="What does degradation mean?" title={DEGRADATION_TOOLTIP}
              aria-describedby={`${summaryId}-deg`}>ⓘ</button>
          </div>
          <div className="mt-1 text-[15px] font-bold" data-testid="degradation-value">{formatDegradation(p.degradation_s_per_lap, p.degradation_ci)}</div>
          <p id={`${summaryId}-deg`} className="mt-1 text-[11px] text-muted">{DEGRADATION_TOOLTIP}</p>
        </div>
        <div className="rounded-xl bg-accent-soft p-3 text-center" data-testid="pit-window-card">
          <div className="text-[10px] font-semibold tracking-[0.12em] text-accent">PIT WINDOW · HEURISTIC</div>
          {w ? (
            <>
              <div className="mt-0.5 text-xl font-extrabold text-accent" data-testid="pit-window-central">
                {w.central_lap != null ? `Lap ${w.central_lap}` : "No central lap"}
              </div>
              <div className="text-xs text-ink-2" data-testid="pit-window-text">{formatWindow(w)}</div>
              <div className="mt-1 text-[11px] text-muted">threshold: best pace + {w.threshold_s.toFixed(1)} s (a heuristic, not a team rule)</div>
            </>
          ) : (
            <div className="mt-1 text-sm text-ink-2" data-testid="pit-window-text">
              No window: {(p.pit_window_reason ?? "unavailable").replace(/_/g, " ")}. The forecast is too uncertain to suggest laps.
            </div>
          )}
        </div>
      </div>

      <div className="mt-3">
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={showExcluded} onChange={(e) => setShowExcluded(e.target.checked)} />
          Show excluded laps (in/out laps, safety car, outliers)
        </label>
        <TireChart prediction={p} showExcluded={showExcluded} describedBy={summaryId} />
        <p id={summaryId} className="sr-only" data-testid="tire-chart-summary">{chartSummary(p)}</p>
      </div>

      <div className="mt-3 flex flex-wrap items-start gap-3 text-xs">
        {w && (
          <details className="rounded-lg border border-line px-2.5 py-1.5" data-testid="confidence">
            <summary className="cursor-pointer">
              Confidence: <b className={w.confidence === "high" ? "text-green" : w.confidence === "medium" ? "text-amber-ink" : "text-red-ink"}>{w.confidence}</b>
            </summary>
            <ul className="mt-1 list-disc pl-4">{w.confidence_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          </details>
        )}
        {w && (
          <details className="rounded-lg border border-line px-2.5 py-1.5" data-testid="assumptions">
            <summary className="cursor-pointer">Assumptions ({w.assumptions.length})</summary>
            <ul className="mt-1 list-disc pl-4">{w.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
          </details>
        )}
        <span className="rounded-lg border border-line px-2.5 py-1.5">
          Remaining competitive laps: <b>{p.remaining_competitive_laps ?? MISSING}</b>
        </span>
      </div>
      {(p.warnings ?? []).length > 0 && (
        <ul className="mt-2 list-disc pl-4 text-[11px] text-muted" data-testid="tire-warnings">
          {(p.warnings ?? []).map((m) => <li key={m}>{m}</li>)}
        </ul>
      )}
      <Observed p={p} />
      {p.model && (
        <p className="mt-3 border-t border-line pt-2 text-[11px] text-muted" data-testid="model-footer">
          Model {p.model.name} · version {p.model.version} · trained through {p.model.trained_through}
        </p>
      )}
    </div>
  );
}
