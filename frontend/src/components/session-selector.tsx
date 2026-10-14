"use client";

import { useState } from "react";

import { isAdmin, useAuth } from "@/lib/auth";
import { useEvents, useIngest, useJob, useSeasons, useSessionStatus } from "@/lib/queries";
import { parseSessionKey, sessionKey } from "@/lib/url-state";

/** Season → Grand Prix → Session. Emits a session key; triggers ingest when needed. */
export function SessionSelector({ value, onChange, demo }: { value: string | null; onChange: (key: string | null) => void; demo: boolean }) {
  const parts = parseSessionKey(value);
  // The URL wins when it names a session (shared links, back/forward); the draft only holds a
  // half-made choice (season or Grand Prix picked, session not yet).
  const [draft, setDraft] = useState<{ year: number | null; round: number | null }>({ year: null, round: null });
  const year = parts?.year ?? draft.year;
  const round = parts?.round ?? draft.round;
  const setYear = (y: number | null) => setDraft({ year: y, round: null });
  const setRound = (r: number | null) => setDraft({ year, round: r });
  const [now] = useState(() => Date.now());

  const seasons = useSeasons();
  const events = useEvents(year);
  const event = events.data?.events.find((e) => e.round === round) ?? null;
  // A session named in the URL is selectable before (or without) the schedule loading.
  const sessionOptions = event
    ? event.sessions.filter((s) => s.code)
    : parts ? [{ name: parts.code, code: parts.code, date: null }] : [];

  return (
    <div>
      <div className="strip" role="group" aria-label="Session picker">
        <div className="strip-cell">
          <label className="field-label" htmlFor="sel-season">Season</label>
          {/* the real control is always rendered (disabled while loading) so its label stays associated */}
          {(
            <select id="sel-season" className="field-select" value={year ?? ""} disabled={!seasons.data}
              aria-busy={seasons.isPending}
              onChange={(e) => { setYear(e.target.value ? Number(e.target.value) : null); onChange(null); }}>
              <option value="">{seasons.isPending ? "Loading seasons…" : seasons.isError ? "Seasons unavailable" : "Choose a season"}</option>
              {!seasons.data && year !== null && <option value={year}>{year}</option>}
              {seasons.data?.seasons.map((s) => (
                <option key={s.year} value={s.year}>{s.year}{s.sessions_stored ? ` · ${s.sessions_stored} stored` : ""}</option>
              ))}
            </select>
          )}
        </div>
        <div className="strip-cell">
          <label className="field-label" htmlFor="sel-event">Grand Prix</label>
          {(
            <select id="sel-event" className="field-select" value={round ?? ""} disabled={year === null || !events.data}
              aria-busy={year !== null && events.isPending}
              onChange={(e) => { setRound(e.target.value ? Number(e.target.value) : null); onChange(null); }}>
              <option value="">{year === null ? "Pick a season first" : events.isPending ? "Loading schedule…" : events.isError ? "Schedule unavailable" : "Choose a Grand Prix"}</option>
              {!events.data && round !== null && <option value={round}>Round {round}</option>}
              {events.data?.events.map((e) => (
                <option key={e.round} value={e.round}>R{String(e.round).padStart(2, "0")} · {e.event_name}</option>
              ))}
            </select>
          )}
        </div>
        <div className="strip-cell">
          <label className="field-label" htmlFor="sel-session">Session</label>
          <select id="sel-session" className="field-select" value={parts && parts.round === round && parts.year === year ? parts.code : ""}
            disabled={sessionOptions.length === 0}
            onChange={(e) => onChange(e.target.value && year && round ? sessionKey({ year, round, code: e.target.value }) : null)}>
            <option value="">{sessionOptions.length ? "Choose a session" : "Pick a Grand Prix first"}</option>
            {sessionOptions.map((s) => {
              const future = s.date ? Date.parse(s.date) > now : false;
              return <option key={s.code} value={s.code!} disabled={future}>{s.name}{future ? " (not run yet)" : ""}</option>;
            })}
          </select>
        </div>
      </div>
      {value && <SessionStatusBar sessionKey={value} demo={demo} />}
    </div>
  );
}

function SessionStatusBar({ sessionKey: key, demo }: { sessionKey: string; demo: boolean }) {
  const status = useSessionStatus(key);
  const ingest = useIngest(key);
  const jobId = ingest.data?.id ?? null;
  const job = useJob(jobId, key);
  const running = ingest.isPending || (jobId !== null && (!job.data || !["succeeded", "failed"].includes(job.data.status)));
  const failed = job.data?.status === "failed" ? job.data : null;
  const user = useAuth((st) => st.user);
  const canIngest = !demo && isAdmin(user);

  if (status.isPending) return <p className="mt-2 text-sm text-muted" role="status">Checking {key}…</p>;
  if (status.isError) return null; // the toast already explains it
  const s = status.data;
  if (s.status === "ready") {
    return (
      <p className="mt-2 text-sm text-muted" role="status" aria-live="polite">
        <span className="chip mr-2">● ready</span>
        {s.info?.event_name} · {s.info?.session_name} · {s.manifest?.lap_count} laps stored
      </p>
    );
  }
  const pct = Math.round((job.data?.progress ?? 0) * 100);
  return (
    <div className="card mt-3 flex flex-wrap items-center gap-4 p-4" aria-live="polite">
      <div className="min-w-[240px] flex-1">
        <div className="text-sm font-semibold">
          {s.status === "stale_schema" ? `${key} was stored by an older version.` : `${key} isn't stored yet.`}
        </div>
        {running ? (
          <>
            <p className="text-sm text-muted" role="status" data-testid="ingest-progress">
              Ingesting from FastF1… {pct}%{job.data?.message ? ` · ${job.data.message}` : ""}
              {job.data?.status === "retrying" ? " (retrying after an error)" : ""}
            </p>
            <div className="mt-2 h-1.5 overflow-hidden rounded bg-line" role="progressbar" aria-label="Ingest progress"
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
              <i className="block h-full bg-[var(--accent)] transition-[width]" style={{ width: `${pct}%` }} />
            </div>
          </>
        ) : failed ? (
          <p className="text-sm text-red-ink" role="alert" data-testid="ingest-failed">
            Ingest failed after {failed.attempts} attempt{failed.attempts === 1 ? "" : "s"}: {failed.error}
          </p>
        ) : (
          <p className="text-sm text-muted" data-testid="ingest-hint">
            {demo ? "Demo mode can't ingest; switch to live data with the backend running."
              : canIngest ? "Download and store it once; later visits load instantly."
                : "Only admins can ingest new sessions. Sign in with an admin account."}
          </p>
        )}
      </div>
      <button type="button" className="btn" disabled={!canIngest || running} onClick={() => ingest.mutate()}>
        {running ? "Ingesting…" : failed ? "Try again" : s.status === "stale_schema" ? "Re-ingest" : "Ingest session"}
      </button>
    </div>
  );
}
