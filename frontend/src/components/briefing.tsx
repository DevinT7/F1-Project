"use client";
/**
 * The longer write-ups each tab carries: the Pit Wall brief, the speed-trap (straights) table, the
 * full corner table and the lap context. Every sentence is assembled from the verified findings the
 * API returned; nothing here is generated text.
 */
import type { ComparisonResponse, Finding } from "@/lib/api/endpoints";
import { lossStyle } from "@/lib/colors";
import { describeGap, formatDelta, formatDiff, formatSpeed, formatTemperature } from "@/lib/format";
import { useUiStore } from "@/lib/store";

import { causeText } from "./findings";

type Ctx = Record<string, unknown>;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

function signed(v: number | null) {
  return v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
}

/** Pit Wall: the whole comparison in a few verified sentences. */
export function RaceBrief({ comparison }: { comparison: ComparisonResponse }) {
  const { reference: ref, target: tgt } = comparison.meta;
  const f = comparison.findings;
  const m = f.lap_summary.metrics ?? {};
  const gap = tgt.lap_time_s - ref.lap_time_s;
  const byId = new Map(f.corners.map((c) => [c.id, c]));
  const losses = f.largest_losses.map((id) => byId.get(id)).filter(Boolean) as Finding[];
  const gains = f.largest_gains.map((id) => byId.get(id)).filter(Boolean) as Finding[];
  const sectors = [m.sector_1_delta_s, m.sector_2_delta_s, m.sector_3_delta_s].map(num);
  const worst = sectors.reduce<number | null>((w, v, i) => (v != null && (w == null || v > sectors[w]!) ? i : w), null);
  const fast = f.straights.reduce<Finding | null>((best, s) => {
    const d = num(s.metrics?.top_speed_diff_kph);
    return d != null && (best == null || Math.abs(d) > Math.abs(num(best.metrics?.top_speed_diff_kph) ?? 0)) ? s : best;
  }, null);
  const counts = { lost: num(m.corners_lost), gained: num(m.corners_gained), unclear: num(m.corners_unclear), total: num(m.corner_count) };

  const lines: { key: string; text: string }[] = [
    { key: "gap", text: describeGap(gap, tgt.driver, ref.driver).replace(/\.?$/, ".") },
  ];
  if (worst != null && sectors[worst] != null) {
    lines.push({ key: "sector", text: `The gap is built mostly in sector ${worst + 1} (${formatDelta(sectors[worst], { withWord: false })} for ${tgt.driver}); `
      + `the three sectors read ${sectors.map((s, i) => `S${i + 1} ${formatDelta(s, { withWord: false })}`).join(", ")}.` });
  }
  if (counts.total != null) {
    lines.push({ key: "corners", text: `Across ${counts.total} corners, ${tgt.driver} lost time in ${counts.lost ?? 0}, gained in ${counts.gained ?? 0}`
      + `${counts.unclear ? ` and ${counts.unclear} are too close to call` : ""}.` });
  }
  if (losses[0]) {
    lines.push({ key: "loss", text: `Biggest loss: ${losses[0].label}, ${formatDelta(losses[0].time_loss_s, { withWord: false })}`
      + `${losses[0].causes?.length ? ` (${losses[0].causes.map(causeText).join(", ")})` : ""}.` });
  }
  if (gains[0]) {
    lines.push({ key: "gain", text: `Best corner for ${tgt.driver}: ${gains[0].label}, ${formatDelta(gains[0].time_loss_s, { withWord: false })}`
      + `${gains[0].causes?.length ? ` (${gains[0].causes.map(causeText).join(", ")})` : ""}.` });
  }
  if (fast) {
    lines.push({ key: "speed", text: `Largest speed-trap difference on ${fast.label}: ${formatDiff(num(fast.metrics?.top_speed_diff_kph), "km/h")} for ${tgt.driver}.` });
  }

  return (
    <section className="card" aria-labelledby="brief-title" data-testid="race-brief">
      <div className="kick">Pit wall brief</div>
      <h2 id="brief-title" className="display mt-1 text-[22px]">{tgt.driver} vs {ref.driver}</h2>
      <ul className="mt-3 space-y-2 text-[15px] leading-relaxed text-ink-2">
        {lines.map((l) => (
          <li key={l.key} className="flex gap-3"><span aria-hidden className="mt-2 h-1.5 w-3 shrink-0 -skew-x-12 bg-accent-strong" />{l.text}</li>
        ))}
      </ul>
      <LapContext comparison={comparison} />
    </section>
  );
}

/** Tyres, temperatures and alignment quality behind the comparison. */
export function LapContext({ comparison }: { comparison: ComparisonResponse }) {
  const ctx = (comparison.findings.session_context ?? {}) as Ctx;
  const side = (s: "reference" | "target") => (ctx[s] ?? {}) as Ctx;
  const q = (comparison.findings.quality ?? {}) as Ctx;
  const tyre = (s: Ctx) => [str(s.compound), num(s.tyre_age_laps) != null ? `${num(s.tyre_age_laps)} laps old` : null,
    s.fresh_tyre === true ? "new set" : null].filter(Boolean).join(" · ") || "—";
  const rows: [string, string, string][] = [
    ["Tyre", tyre(side("reference")), tyre(side("target"))],
    ["Air / track", `${formatTemperature(num(side("reference").air_temp_c))} / ${formatTemperature(num(side("reference").track_temp_c))}`,
      `${formatTemperature(num(side("target").air_temp_c))} / ${formatTemperature(num(side("target").track_temp_c))}`],
  ];
  const { reference: ref, target: tgt } = comparison.meta;
  return (
    <div className="mt-5 grid gap-4 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <table className="data-table" aria-label="Lap conditions">
        <thead><tr><th scope="col">Conditions</th><th scope="col">{ref.driver}</th><th scope="col">{tgt.driver}</th></tr></thead>
        <tbody>
          {rows.map(([label, a, b]) => (
            <tr key={label}><td className="text-muted">{label}</td><td>{a}</td><td>{b}</td></tr>
          ))}
        </tbody>
      </table>
      <div className="rounded-lg border border-line bg-card-2 p-3 text-xs text-muted" data-testid="alignment-quality">
        <div className="kick mb-1">Alignment</div>
        Laps aligned by {str(q.alignment) === "xy" ? "track position (X/Y)" : "distance"}
        {num(q.xy_correction_rms_m) != null ? `, ${num(q.xy_correction_rms_m)!.toFixed(1)} m RMS correction` : ""};
        {" "}{num(q.sample_count_ref) ?? "—"} / {num(q.sample_count_target) ?? "—"} telemetry samples.
        {(comparison.findings.notes ?? []).length > 0 && (
          <ul className="mt-2 list-disc space-y-0.5 pl-4">{(comparison.findings.notes ?? []).map((n) => <li key={n}>{n}</li>)}</ul>
        )}
      </div>
    </div>
  );
}

/** Lap Comparison: straights as speed traps. */
export function StraightsTable({ comparison }: { comparison: ComparisonResponse }) {
  const { reference: ref, target: tgt } = comparison.meta;
  return (
    <section className="card" aria-labelledby="straights-title" data-testid="straights-table">
      <div className="kick">Speed traps</div>
      <h2 id="straights-title" className="text-[16px] font-bold">Straight-line speed, straight by straight</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Straight</th><th scope="col">{ref.driver} top</th><th scope="col">{tgt.driver} top</th>
              <th scope="col">Δ speed</th><th scope="col">Δ exit</th><th scope="col">Δ time</th>
            </tr>
          </thead>
          <tbody>
            {comparison.findings.straights.map((s) => {
              const dt = num(s.metrics?.time_diff_s) ?? s.time_loss_s ?? null;
              const dv = num(s.metrics?.top_speed_diff_kph);
              return (
                <tr key={s.id}>
                  <td className="font-semibold">{s.label}</td>
                  <td>{formatSpeed(num(s.reference?.top_speed_kph))}</td>
                  <td>{formatSpeed(num(s.target?.top_speed_kph))}</td>
                  <td className={signed(dv == null ? null : -dv)}>{formatDiff(dv, "km/h")}</td>
                  <td>{formatDiff(num(s.metrics?.exit_speed_diff_kph), "km/h")}</td>
                  <td className={signed(dt)}>{formatDelta(dt, { withWord: false })}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-muted">Δ = {tgt.driver} − {ref.driver}. A positive time is time {tgt.driver} lost on that straight.</p>
    </section>
  );
}

/** Track Map: every corner in one sortable-by-eye table; a row selects the corner on the map. */
export function CornerTable({ comparison }: { comparison: ComparisonResponse }) {
  const selected = useUiStore((s) => s.selectedCornerId);
  const selectCorner = useUiStore((s) => s.selectCorner);
  return (
    <section className="card" aria-labelledby="corner-table-title" data-testid="corner-table">
      <div className="kick">Corner by corner</div>
      <h2 id="corner-table-title" className="text-[16px] font-bold">All {comparison.findings.corners.length} corners</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Corner</th><th scope="col">Type</th><th scope="col">Δ time</th><th scope="col">Entry</th>
              <th scope="col">Apex</th><th scope="col">Exit</th><th scope="col">Braking</th><th scope="col">Why</th>
            </tr>
          </thead>
          <tbody>
            {comparison.findings.corners.map((c) => {
              const style = lossStyle(c.time_loss_s);
              const m = c.metrics ?? {};
              return (
                <tr key={c.id} onClick={() => selectCorner(c.id)} aria-selected={selected === c.id}
                  className={`cursor-pointer ${selected === c.id ? "bg-accent-soft" : ""}`} data-testid={`corner-row-${c.corner_number ?? c.id}`}>
                  <td className="font-semibold">
                    <button type="button" className="text-left hover:text-accent" onClick={(e) => { e.stopPropagation(); selectCorner(c.id); }}>
                      {c.label}
                    </button>
                  </td>
                  <td className="text-muted capitalize">{c.corner_type ?? "—"}</td>
                  <td className={style.kind === "loss" ? "pos" : style.kind === "gain" ? "neg" : ""}>
                    <span aria-hidden>{style.glyph} </span>{formatDelta(c.time_loss_s, { withWord: false })}
                  </td>
                  <td>{formatDiff(num(m.entry_speed_diff_kph), "km/h")}</td>
                  <td>{formatDiff(num(m.min_speed_diff_kph), "km/h")}</td>
                  <td>{formatDiff(num(m.exit_speed_diff_kph), "km/h")}</td>
                  <td>{formatDiff(num(m.braking_start_diff_m), "m", 0)}</td>
                  <td className="max-w-[260px] truncate text-left text-xs text-muted" title={(c.causes ?? []).map(causeText).join(", ")}>
                    {(c.causes ?? []).length ? (c.causes ?? []).map(causeText).join(" · ") : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-muted">Speed and braking Δ are target − reference; causes are rules whose thresholds were met, not proof.</p>
    </section>
  );
}
