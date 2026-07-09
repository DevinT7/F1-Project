import { useState } from "react";
import {
  Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "./api";
import { mockComparison, mockTires } from "./mock";
import type { AIReport, LapComparison, TireAnalysis } from "./types";

const YOU = "#3b7dd8";   // selected driver (blue)
const REF = "#c1553a";   // reference driver (terracotta)
const GREEN = "#3f9d6d";
const RED = "#d1543f";

export function Dashboard() {
  const [sessionKey, setSessionKey] = useState(9159);
  const [driver, setDriver] = useState(1);
  const [reference, setReference] = useState(4);
  const [cmp, setCmp] = useState<LapComparison | null>(null);
  const [tires, setTires] = useState<TireAnalysis | null>(null);
  const [ai, setAi] = useState<AIReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function analyze(useMock = false) {
    setError(null);
    setLoading(true);
    try {
      if (useMock) {
        const c = mockComparison();
        const t = mockTires();
        setCmp(c); setTires(t); setAi(null);
      } else {
        const [c, t] = await Promise.all([
          api.compare(sessionKey, driver, reference),
          api.tires(sessionKey, driver),
        ]);
        setCmp(c); setTires(t);
        setAi(await api.aiReport(c, t));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      <aside className="side">
        <div className="brand"><div className="logo"><span>RIQ</span></div>
          <div><h1>RACE<b>IQ</b></h1><small>AI RACE ENGINEER</small></div></div>
        <div className="navlabel">TELEMETRY</div>
        <nav className="nav">
          <a className="active">▦ Pit Wall</a>
          <a>⇆ Lap Comparison</a>
          <a>◎ Track Map</a>
          <a>◍ Tire Analysis</a>
          <a>✦ AI Engineer</a>
        </nav>
      </aside>

      <main className="main">
        <div className="topbar">
          <div>
            <div className="eyebrow">◣ RACE ANALYSIS</div>
            <div className="title">Pit Wall</div>
          </div>
        </div>

        {/* Controls */}
        <div className="selectors">
          <label className="sel">SESSION KEY
            <input value={sessionKey} onChange={(e) => setSessionKey(+e.target.value)} />
          </label>
          <label className="sel you">DRIVER #
            <input value={driver} onChange={(e) => setDriver(+e.target.value)} />
          </label>
          <label className="sel cmp">COMPARE #
            <input value={reference} onChange={(e) => setReference(+e.target.value)} />
          </label>
          <div className="sel" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="btn" onClick={() => analyze(false)} disabled={loading}>
              {loading ? "Analyzing…" : "Analyze"}
            </button>
            <button className="btn ghost" onClick={() => analyze(true)}>Demo data</button>
          </div>
        </div>

        {error && <div className="card error">⚠ {error}<div className="hint">
          Is the backend running on :8000? Try “Demo data” to preview the UI.</div></div>}

        {cmp && (
          <>
            <KPIs cmp={cmp} tires={tires} />
            <div className="grid">
              <TelemetryCard cmp={cmp} />
              <div className="grid-col">
                <SectorCard cmp={cmp} />
                {tires && <TireCard tires={tires} />}
              </div>
            </div>
            <div className="grid">
              <CornerCard cmp={cmp} />
              <AICard ai={ai} />
            </div>
          </>
        )}

        {!cmp && !loading && (
          <div className="card empty">Enter a session + two driver numbers and hit
            <b> Analyze</b> — or click <b>Demo data</b> to preview with sample telemetry.</div>
        )}

        <div className="note">Data: OpenF1 + FastF1 · figures computed by the backend</div>
      </main>
    </div>
  );
}

function fmtLap(s?: number | null) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}

function KPIs({ cmp, tires }: { cmp: LapComparison; tires: TireAnalysis | null }) {
  const d = cmp.total_delta_s;
  return (
    <div className="kpis">
      <div className="kpi k1"><div className="lab">BEST LAP</div>
        <div className="num">{fmtLap(cmp.lap_time)}</div>
        <div className="sub">{cmp.driver.name_acronym ?? cmp.driver.driver_number}</div></div>
      <div className="kpi k2"><div className="lab">LAP DELTA</div>
        <div className="num">{d < 0 ? "−" : "+"}{Math.abs(d).toFixed(3)}</div>
        <div className="sub">vs {cmp.reference.name_acronym ?? cmp.reference.driver_number}</div></div>
      <div className="kpi k3"><div className="lab">TIME GAINED</div>
        <div className="num">{cmp.time_gained_s.toFixed(3)}</div><div className="sub">seconds</div></div>
      <div className="kpi k4"><div className="lab">TIRE DEG</div>
        <div className="num">{tires ? tires.degradation_s_per_lap.toFixed(3) : "—"}</div>
        <div className="sub">{tires?.compound ?? ""} s/lap</div></div>
      <div className="kpi k5"><div className="lab">PIT WINDOW</div>
        <div className="num">{tires?.recommended_pit_window
          ? `${tires.recommended_pit_window[0]}–${tires.recommended_pit_window[1]}` : "—"}</div>
        <div className="sub">recommended</div></div>
    </div>
  );
}

function channel(cmp: LapComparison, key: "speed" | "throttle") {
  const a = cmp.driver_trace?.samples ?? [];
  const b = cmp.reference_trace?.samples ?? [];
  return a.map((s, i) => ({
    distance: Math.round(s.distance),
    you: s[key], ref: b[i]?.[key],
  }));
}

function TelemetryCard({ cmp }: { cmp: LapComparison }) {
  const speed = channel(cmp, "speed");
  const thr = channel(cmp, "throttle");
  const delta = cmp.delta_trace.map((p) => ({ distance: Math.round(p.distance), d: p.delta_s }));
  return (
    <div className="card">
      <div className="kick">▮ TELEMETRY TRACE</div>
      <h3>{cmp.driver.name_acronym} vs {cmp.reference.name_acronym}</h3>
      <MiniLine data={speed} label="SPEED km/h" />
      <MiniLine data={thr} label="THROTTLE %" />
      <div className="mlab">Δ TIME s</div>
      <ResponsiveContainer width="100%" height={70}>
        <AreaChart data={delta}>
          <XAxis dataKey="distance" hide /><YAxis hide />
          <Tooltip contentStyle={tip} />
          <Area dataKey="d" stroke={GREEN} fill="rgba(63,157,109,.15)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function MiniLine({ data, label }: { data: any[]; label: string }) {
  return (
    <div>
      <div className="mlab">{label}</div>
      <ResponsiveContainer width="100%" height={70}>
        <LineChart data={data}>
          <CartesianGrid stroke="#ececea" vertical={false} />
          <XAxis dataKey="distance" hide /><YAxis hide />
          <Tooltip contentStyle={tip} />
          <Line dataKey="you" stroke={YOU} dot={false} strokeWidth={1.8} />
          <Line dataKey="ref" stroke={REF} dot={false} strokeWidth={1.3} strokeDasharray="3 2" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function SectorCard({ cmp }: { cmp: LapComparison }) {
  return (
    <div className="card">
      <div className="kick">▮ SECTOR DELTA</div>
      {cmp.sector_deltas.length === 0 && <div className="sub">No sector times available.</div>}
      {cmp.sector_deltas.map((s) => (
        <div className="sd-row" key={s.sector}>
          <span>Sector {s.sector}</span>
          <b style={{ color: s.delta_s < 0 ? GREEN : RED }}>
            {s.delta_s < 0 ? "−" : "+"}{Math.abs(s.delta_s).toFixed(3)}</b>
        </div>
      ))}
      <div className="sd-row total"><span>TOTAL Δ</span>
        <b style={{ color: cmp.total_delta_s < 0 ? GREEN : RED }}>
          {cmp.total_delta_s < 0 ? "−" : "+"}{Math.abs(cmp.total_delta_s).toFixed(3)}</b></div>
    </div>
  );
}

function TireCard({ tires }: { tires: TireAnalysis }) {
  return (
    <div className="card">
      <div className="kick">▮ TIRE ANALYSIS</div>
      <div className="tire-top">
        <div className="tire-badge">{(tires.compound ?? "?")[0]}</div>
        <div><b>{tires.compound ?? "—"} · {tires.tyre_age_laps ?? "?"} laps</b>
          <div className="sub">Deg {tires.degradation_s_per_lap.toFixed(3)} s/lap ·
            conf {(tires.model_confidence * 100).toFixed(0)}%</div></div>
      </div>
      {tires.predictions.slice(0, 4).map((p) => (
        <div className="sd-row" key={p.laps_ahead}>
          <span>+{p.laps_ahead} laps</span><b>{fmtLap(p.predicted_lap_time_s)}</b>
        </div>
      ))}
    </div>
  );
}

function CornerCard({ cmp }: { cmp: LapComparison }) {
  const rows = [...cmp.corners].sort((a, b) => b.time_delta_s - a.time_delta_s).slice(0, 8);
  return (
    <div className="card">
      <div className="kick">▮ CORNER ANALYSIS</div>
      {rows.length === 0 && <div className="sub">No corners detected.</div>}
      {rows.map((c) => (
        <div className="sd-row" key={c.corner_number}>
          <span>Turn {c.corner_number}
            <small className="sub"> · min {c.min_speed?.toFixed(0)} km/h</small></span>
          <b style={{ color: c.time_delta_s < 0 ? GREEN : RED }}>
            {c.time_delta_s < 0 ? "−" : "+"}{Math.abs(c.time_delta_s).toFixed(3)}</b>
        </div>
      ))}
    </div>
  );
}

function AICard({ ai }: { ai: AIReport | null }) {
  return (
    <div className="card">
      <div className="ai-badge">✦ AI ENGINEER {ai ? `· ${ai.source}` : ""}</div>
      {!ai && <div className="sub">Run Analyze (not Demo) with the backend up to generate
        a grounded report.</div>}
      {ai && <>
        <p className="ai-summary">{ai.summary}</p>
        <div className="mlab">RECOMMENDATIONS</div>
        {ai.recommendations.map((r, i) => (
          <div className="reco" key={i}><span className="n">{i + 1}</span>{r}</div>))}
        <div className="mlab">STRATEGY</div>
        {ai.strategy_options.map((s, i) => <div className="reco" key={i}>▸ {s}</div>)}
      </>}
    </div>
  );
}

const tip = { background: "#ffffff", border: "1px solid #ececea", borderRadius: 8,
  color: "#1c1b1a", fontSize: 12 };
