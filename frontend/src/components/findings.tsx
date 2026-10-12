"use client";
/** CornerPanel (the original dashboard's corner card), FindingsList and the sector-delta card. */
import type { ComparisonResponse, Finding } from "@/lib/api/endpoints";
import { lossStyle } from "@/lib/colors";
import {
  MISSING, formatDelta, formatDiff, formatDistance, formatGear, formatSeconds, formatSpeed,
} from "@/lib/format";
import { useUiStore } from "@/lib/store";

type Values = NonNullable<Finding["reference"]>;
const num = (v: Values[string] | undefined): number | null => (typeof v === "number" ? v : null);

const CAUSE_TEXT: Record<string, string> = {
  early_braking_loss: "braked earlier",
  late_braking_gain: "braked later",
  low_min_speed: "lower minimum speed",
  high_min_speed: "higher minimum speed",
  late_throttle: "later to full throttle",
  early_throttle: "earlier to full throttle",
  lower_exit_speed: "lower exit speed",
  higher_exit_speed: "higher exit speed",
  straight_line_deficit: "slower on the straight",
  straight_line_advantage: "faster on the straight",
  unclear: "no single rule explains it",
};

export function causeText(cause: string): string {
  return CAUSE_TEXT[cause] ?? cause.replace(/_/g, " ");
}

/** Selected corner, or the biggest loss when nothing is selected yet. */
export function useSelectedCorner(cmp: ComparisonResponse): Finding | null {
  const selected = useUiStore((s) => s.selectedCornerId);
  const corners = cmp.findings.corners;
  return corners.find((f) => f.id === selected)
    ?? corners.find((f) => f.id === cmp.findings.largest_losses[0])
    ?? corners[0] ?? null;
}

interface Row {
  label: string;
  ref: string;
  tgt: string;
  diff: string;
  diffValue: number | null;
  /** +1 when a positive diff means the target lost time (e.g. later full throttle), −1 when it means gained */
  lossSign: 1 | -1;
}

export function cornerRows(f: Finding): Row[] {
  const r = f.reference ?? {}, t = f.target ?? {}, m = f.metrics ?? {};
  return [
    { label: "Entry speed", ref: formatSpeed(num(r.entry_speed_kph)), tgt: formatSpeed(num(t.entry_speed_kph)),
      diff: formatDiff(num(m.entry_speed_diff_kph), "km/h"), diffValue: num(m.entry_speed_diff_kph), lossSign: -1 },
    { label: "Minimum speed", ref: formatSpeed(num(r.min_speed_kph)), tgt: formatSpeed(num(t.min_speed_kph)),
      diff: formatDiff(num(m.min_speed_diff_kph), "km/h"), diffValue: num(m.min_speed_diff_kph), lossSign: -1 },
    { label: "Exit speed", ref: formatSpeed(num(r.exit_speed_kph)), tgt: formatSpeed(num(t.exit_speed_kph)),
      diff: formatDiff(num(m.exit_speed_diff_kph), "km/h"), diffValue: num(m.exit_speed_diff_kph), lossSign: -1 },
    { label: "Braking point", ref: formatDistance(num(r.braking_start_m)), tgt: formatDistance(num(t.braking_start_m)),
      diff: formatDiff(num(m.braking_start_diff_m), "m", 0), diffValue: num(m.braking_start_diff_m), lossSign: -1 },
    { label: "Throttle pick-up", ref: formatDistance(num(r.throttle_reapply_m)), tgt: formatDistance(num(t.throttle_reapply_m)),
      diff: formatDiff(num(m.throttle_reapply_diff_m), "m", 0), diffValue: num(m.throttle_reapply_diff_m), lossSign: 1 },
    { label: "To full throttle", ref: formatSeconds(num(r.time_to_full_throttle_s), 2), tgt: formatSeconds(num(t.time_to_full_throttle_s), 2),
      diff: formatDiff(num(m.time_to_full_throttle_diff_s), "s", 2), diffValue: num(m.time_to_full_throttle_diff_s), lossSign: 1 },
    { label: "Lowest gear", ref: formatGear(num(r.gear_min)), tgt: formatGear(num(t.gear_min)),
      diff: formatDiff(num(m.gear_min_diff), "", 0), diffValue: null, lossSign: 1 },
  ];
}

export function CornerPanel({ comparison }: { comparison: ComparisonResponse }) {
  const f = useSelectedCorner(comparison);
  const { reference, target } = comparison.meta;
  if (!f) return <div className="text-sm text-muted">No corners detected.</div>;
  const style = lossStyle(f.time_loss_s);
  const pill = style.kind === "loss" ? "bg-red-soft text-red-ink" : style.kind === "gain" ? "bg-green-soft text-green-ink" : "bg-card-2 text-muted";

  return (
    <section className="rounded-xl border border-line bg-card-2 p-4" aria-labelledby="corner-title" aria-live="polite" data-testid="corner-panel">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 id="corner-title" className="text-[15px] font-bold">{f.label}</h3>
          <div className="text-[11px] text-muted">
            {f.corner_type ? `${f.corner_type[0]!.toUpperCase()}${f.corner_type.slice(1)}-speed` : "Corner"}
            {f.apex_source && f.apex_source !== "matched" ? ` · apex ${f.apex_source}` : ""} · {f.confidence} confidence
          </div>
        </div>
        <span className="chip" title="Rank by |time difference| among all corners">#{f.rank ?? MISSING}</span>
      </div>
      <table className="mt-3 w-full text-[12.5px]">
        <caption className="sr-only">{f.label}: reference {reference.driver}, target {target.driver}, difference target − reference</caption>
        <thead>
          <tr className="text-left text-[10px] tracking-wider text-muted uppercase">
            <th scope="col" className="pb-1 font-semibold">Metric</th>
            <th scope="col" className="pb-1 text-right font-semibold">{reference.driver}</th>
            <th scope="col" className="pb-1 text-right font-semibold">{target.driver}</th>
            <th scope="col" className="pb-1 text-right font-semibold">Δ</th>
          </tr>
        </thead>
        <tbody>
          {cornerRows(f).map((row) => {
            const tone = row.diffValue === null || row.diffValue === 0 ? "" : row.diffValue * row.lossSign > 0 ? "pos" : "neg";
            return (
              <tr key={row.label} className="border-t border-line">
                <th scope="row" className="py-1.5 text-left font-normal text-muted">{row.label}</th>
                <td className="num-tab py-1.5 text-right font-semibold">{row.ref}</td>
                <td className="num-tab py-1.5 text-right font-semibold">{row.tgt}</td>
                <td className={`num-tab py-1.5 text-right font-semibold ${tone}`}>{row.diff}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className={`mt-3 rounded-lg p-2 text-center text-[13px] font-bold ${pill}`} data-testid="corner-delta">
        <span aria-hidden>{style.glyph} </span>Δ {formatDelta(f.time_loss_s)}
        <div className="text-[11px] font-medium">entry {formatDelta(f.entry_loss_s, { withWord: false })} · exit {formatDelta(f.exit_loss_s, { withWord: false })}</div>
      </div>
      {(f.causes ?? []).length > 0 && (
        <div className="mt-3">
          <div className="kick">Contributing factors</div>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {(f.causes ?? []).map((c) => <li key={c} className="chip">{causeText(c)}</li>)}
          </ul>
        </div>
      )}
      {(f.notes ?? []).length > 0 && (
        <ul className="mt-2 list-disc pl-4 text-[11px] text-muted">
          {(f.notes ?? []).map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
    </section>
  );
}

export function FindingsList({ comparison }: { comparison: ComparisonResponse }) {
  const selectCorner = useUiStore((s) => s.selectCorner);
  const selected = useUiStore((s) => s.selectedCornerId);
  const byId = new Map(comparison.findings.corners.map((f) => [f.id, f]));
  const group = (title: string, ids: string[], empty: string) => (
    <div>
      <div className="kick mt-1">{title}</div>
      {ids.length === 0 && <p className="py-2 text-sm text-muted">{empty}</p>}
      <ol className="mt-1">
        {ids.map((id) => {
          const f = byId.get(id);
          if (!f) return null;
          const style = lossStyle(f.time_loss_s);
          return (
            <li key={id}>
              <button type="button" onClick={() => selectCorner(id)} aria-pressed={selected === id}
                className={`sd-row w-full rounded-md px-1 text-left hover:bg-card-2 ${selected === id ? "bg-card-2" : ""}`}>
                <span className="min-w-0">
                  <span className="font-semibold">{f.label}</span>
                  <span className="block truncate text-[11px] text-muted">
                    {f.causes?.length ? f.causes.map(causeText).join(" · ") : "below the noise floor"}
                  </span>
                </span>
                <b className={`num-tab shrink-0 ${style.kind === "loss" ? "pos" : style.kind === "gain" ? "neg" : ""}`}>
                  <span aria-hidden>{style.glyph} </span>{formatDelta(f.time_loss_s, { withWord: false })}
                </b>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
  return (
    <section className="card" aria-labelledby="findings-title" data-testid="findings-list">
      <div className="kick">Verified findings</div>
      <h2 id="findings-title" className="text-[15px] font-bold">Where {comparison.meta.target.driver} lost and gained time</h2>
      <div className="mt-2 grid gap-4 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
        {group("Largest losses", comparison.findings.largest_losses, "No corner lost more than 0.02 s.")}
        {group("Largest gains", comparison.findings.largest_gains, "No corner gained more than 0.02 s.")}
      </div>
      <p className="mt-3 text-[11px] text-muted">Causes are rules whose thresholds were met — contributing factors, not proof.</p>
    </section>
  );
}

export function SectorCard({ comparison }: { comparison: ComparisonResponse }) {
  const sectors = comparison.findings.sectors;
  const total = comparison.findings.lap_summary.time_loss_s;
  return (
    <section className="card" aria-labelledby="sector-title">
      <div className="kick">Sector delta</div>
      <h2 id="sector-title" className="sr-only">Sector delta</h2>
      {sectors.length === 0 && <p className="text-sm text-muted">No official sector times for one of these laps.</p>}
      {sectors.map((s) => {
        const official = num(s.metrics?.official_sector_delta_s);
        const v = official ?? s.time_loss_s ?? null;
        return (
          <div className="sd-row" key={s.id}>
            <span>{s.label}{s.notes?.length ? <span className="text-muted" title={s.notes.join(" ")}> *</span> : null}</span>
            <b className={`num-tab ${v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""}`}>{formatDelta(v, { withWord: false })}</b>
          </div>
        );
      })}
      <div className="sd-row total">
        <span>Total Δ</span>
        <b className={`num-tab ${(total ?? 0) > 0 ? "pos" : "neg"}`}>{formatDelta(total, { withWord: false })}</b>
      </div>
    </section>
  );
}
