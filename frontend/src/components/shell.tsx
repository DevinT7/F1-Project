"use client";
/** App shell pieces carried over from the original pit-wall dashboard: sidebar, topbar, theme. */
import Link from "next/link";
import { useEffect, type ReactNode } from "react";

import { useUiStore, type Theme } from "@/lib/store";

const NAV = [
  { href: "#comparison", icon: "▦", label: "Pit Wall" },
  { href: "#telemetry", icon: "⇆", label: "Lap Comparison" },
  { href: "#track", icon: "◎", label: "Track Map" },
  { href: "#tires", icon: "◍", label: "Tire Analysis" },
  { href: "#engineer", icon: "✦", label: "AI Engineer" },
] as const;

export function Sidebar({ sessionLabel }: { sessionLabel: string | null }) {
  return (
    <aside className="sticky top-0 hidden h-screen flex-col gap-1.5 border-r border-line px-4 py-5 lg:flex" aria-label="Sidebar">
      <div className="flex items-center gap-3 px-1.5 pb-4">
        <div aria-hidden className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] bg-gradient-to-br from-[#1c1b1a] to-[#454140] text-xs font-extrabold text-white ring-1 ring-line">
          RIQ
        </div>
        <div>
          <div className="text-[15px] font-bold">RACE<span className="text-accent">IQ</span></div>
          <div className="text-[11px] text-muted">AI Race Engineer</div>
        </div>
      </div>
      <div className="kick px-2 pt-3 pb-1.5">Telemetry</div>
      <nav aria-label="Sections" className="flex flex-col gap-0.5">
        {NAV.map((item, i) => (
          <a key={item.href} href={item.href}
            className={`flex items-center gap-3 rounded-[10px] px-3 py-2.5 text-sm font-medium ${
              i === 0 ? "bg-[var(--nav-active)] text-[var(--nav-active-ink)]" : "text-ink-2 hover:bg-card-2"}`}>
            <span aria-hidden className="w-4 text-center opacity-80">{item.icon}</span>{item.label}
          </a>
        ))}
      </nav>
      <div className="mt-auto rounded-xl border border-line p-3">
        <div className="kick">Session</div>
        <div className="mt-0.5 text-[13px] font-semibold">{sessionLabel ?? "None selected"}</div>
      </div>
    </aside>
  );
}

export function ThemeToggle() {
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);

  useEffect(() => {
    const saved = document.documentElement.dataset.theme;
    if (saved === "light" || saved === "dark") setTheme(saved);
  }, [setTheme]);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("raceiq-theme", next); } catch { /* private mode */ }
    setTheme(next);
  }

  return (
    <button type="button" className="btn btn-ghost" onClick={toggle} aria-pressed={theme === "light"}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
      <span aria-hidden>{theme === "dark" ? "☀" : "☾"}</span> Theme
    </button>
  );
}

export function Topbar({ demo, demoHref, liveHref, season }: { demo: boolean; demoHref: string; liveHref: string; season: number | null }) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="eyebrow">◣ Race analysis{season ? ` · ${season} season` : ""}</div>
        <h1 className="mt-1 font-serif text-[34px] italic leading-tight">Pit Wall</h1>
      </div>
      <div className="flex flex-wrap gap-2.5">
        <ThemeToggle />
        {demo
          ? <Link className="btn btn-ghost" href={liveHref}>⏻ Live data</Link>
          : <Link className="btn btn-ghost" href={demoHref}>▶ Demo data</Link>}
      </div>
    </header>
  );
}

/** Card for panels owned by later phases, gated by a feature flag (src/lib/flags.ts). */
export function PlaceholderCard({ id, kick, title, enabled, children }: {
  id: string; kick: string; title: string; enabled: boolean; children?: ReactNode;
}) {
  return (
    <section id={id} className="card" aria-labelledby={`${id}-title`}>
      <div className="kick">{kick}</div>
      <h2 id={`${id}-title`} className="mt-1 text-[15px] font-bold">{title}</h2>
      {enabled && children ? children : (
        <div className="mt-3 rounded-xl border border-dashed border-line bg-card-2 p-4 text-sm text-muted">
          <span className="chip mb-2">Coming in a later phase</span>
          <p>This panel is wired to a feature flag and will light up when its phase ships.</p>
        </div>
      )}
    </section>
  );
}

export function Toaster() {
  const toasts = useUiStore((s) => s.toasts);
  const dismiss = useUiStore((s) => s.dismissToast);
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2"
      role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className="card pointer-events-auto border-red bg-red-soft p-3.5 text-sm" data-testid="toast">
          <div className="flex items-start justify-between gap-3">
            <div className="font-bold text-red-ink">⚠ {t.title}</div>
            <button type="button" className="text-muted" onClick={() => dismiss(t.id)} aria-label="Dismiss notification">✕</button>
          </div>
          <p className="mt-1 text-ink-2">{t.message}</p>
          <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
            {t.code && <span className="chip">code: {t.code}</span>}
            {t.requestId && <span className="chip">request_id: {t.requestId}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}
