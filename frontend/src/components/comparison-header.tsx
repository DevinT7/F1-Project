"use client";
/** KPI strip from the original dashboard, now driven by the comparison: laps, signed gap, top speed, sectors. */
import type { ComparisonResponse } from "@/lib/api/endpoints";
import { driverStyles, useThemePalette } from "@/lib/chart-colors";
import { describeGap, formatDelta, formatDiff, formatLapTime } from "@/lib/format";

export function ComparisonHeader({ comparison }: { comparison: ComparisonResponse }) {
  const palette = useThemePalette();
  const styles = driverStyles(comparison, palette);
  const { reference, target } = comparison.meta;
  const gap = target.lap_time_s - reference.lap_time_s;
  const summary = comparison.findings.lap_summary.metrics ?? {};
  const sectors = [summary.sector_1_delta_s, summary.sector_2_delta_s, summary.sector_3_delta_s];
  const gapText = describeGap(gap, target.driver, reference.driver);

  return (
    <section id="comparison" className="kpis" aria-label="Comparison summary">
      <div className="kpi">
        <div className="lab"><Swatch color={styles.reference.color} />Reference</div>
        <div className="num">{formatLapTime(reference.lap_time_s)}</div>
        <div className="sub">{reference.driver} · L{reference.lap_number}{reference.team ? ` · ${reference.team}` : ""}</div>
      </div>
      <div className="kpi">
        <div className="lab"><Swatch color={styles.target.color} dashed={styles.target.dash === "dash"} />Target</div>
        <div className="num">{formatLapTime(target.lap_time_s)}</div>
        <div className="sub">{target.driver} · L{target.lap_number}{target.team ? ` · ${target.team}` : ""}</div>
      </div>
      <div className="kpi accent">
        <div className="lab">
          Gap <span className="cursor-help underline decoration-dotted" title={gapText} tabIndex={0} role="note" aria-label={gapText}>ⓘ</span>
        </div>
        <div className={`num ${gap > 0 ? "pos" : gap < 0 ? "neg" : ""}`} data-testid="lap-gap">{formatDelta(gap, { withWord: false })}</div>
        <div className="sub">{formatDelta(gap).replace(/^.* \(/, "target ").replace(")", "")} than {reference.driver}</div>
      </div>
      <div className="kpi blue">
        <div className="lab">Top speed Δ</div>
        <div className="num">{formatDiff(summary.top_speed_diff_kph, "", 1)}</div>
        <div className="sub">km/h · target − reference</div>
      </div>
      <div className="kpi amber">
        <div className="lab">Sectors</div>
        <div className="mt-2 flex flex-col gap-0.5 text-sm font-semibold">
          {sectors.map((s, i) => (
            <span key={i} className="num-tab flex justify-between gap-3">
              <span className="text-muted">S{i + 1}</span>
              <span className={s == null ? "" : s > 0 ? "pos" : s < 0 ? "neg" : ""}>{formatDelta(s, { withWord: false })}</span>
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function Swatch({ color, dashed = false }: { color: string; dashed?: boolean }) {
  return (
    <span aria-hidden className="mr-1.5 inline-block h-0 w-3 align-middle"
      style={{ borderTop: `3px ${dashed ? "dashed" : "solid"} ${color}` }} />
  );
}
