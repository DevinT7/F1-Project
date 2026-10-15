"use client";

import { useState } from "react";

import { useEvents, useSeasons, useSessionStatus } from "@/lib/queries";
import { parseSessionKey, sessionKey } from "@/lib/url-state";

/** Season → Grand Prix → Session. Emits a session key; says how to add a session that isn't stored. */
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
  const parts = parseSessionKey(key);
  return (
    <div className="card mt-3 p-4" aria-live="polite" data-testid="session-not-stored">
      <div className="text-sm font-semibold">
        {s.status === "stale_schema" ? `${key} was stored by an older version.` : `${key} isn't stored yet.`}
      </div>
      <p className="mt-1 text-sm text-muted" data-testid="ingest-hint">
        {demo ? "Demo mode only has 2024 China qualifying; switch to live data with the backend running."
          : "Sessions are added by the operator from the command line:"}
      </p>
      {!demo && parts && (
        <code className="mt-2 block overflow-x-auto rounded-md border border-line bg-card-2 px-3 py-2 text-xs text-ink-2">
          python -m app.ingestion ingest --year {parts.year} --round {parts.round} --session {parts.code}{s.status === "stale_schema" ? " --force" : ""}
        </code>
      )}
    </div>
  );
}
