"use client";
/** App shell: the top bar (Senna wordmark, the five tabs, account + data source), placeholder card, toasts. */
import Link from "next/link";
import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { useUiStore } from "@/lib/store";
import { TABS, type TabId } from "@/lib/tabs";

export function Wordmark() {
  return (
    <div className="flex items-center" aria-label="Senna">
      <span className="display text-[24px] tracking-[0.08em]" aria-hidden>
        SENN<span className="text-accent">A</span>
      </span>
    </div>
  );
}

/** Tabs as words; the selected one is underlined by a bar that slides between them. */
export function TabBar({ tab, onTab }: { tab: TabId; onTab: (tab: TabId) => void }) {
  const refs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  const [bar, setBar] = useState<{ left: number; width: number } | null>(null);

  const place = useCallback(() => {
    const el = refs.current[tab];
    if (el) setBar({ left: el.offsetLeft, width: el.offsetWidth });
  }, [tab]);

  useLayoutEffect(() => {
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [place]);

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1
      : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : null;
    if (next === null) return;
    e.preventDefault();
    const t = TABS[(next + TABS.length) % TABS.length]!;
    onTab(t.id);
    refs.current[t.id]?.focus();
  }

  return (
    <div role="tablist" aria-label="Sections" className="relative flex gap-7 overflow-x-auto" onKeyDown={onKey}>
      {TABS.map((t) => (
        <button key={t.id} ref={(el) => { refs.current[t.id] = el; }} type="button" role="tab"
          id={`tab-${t.id}`} aria-selected={t.id === tab} aria-controls="tab-panel" tabIndex={t.id === tab ? 0 : -1}
          className="tab-btn" data-testid={`tab-${t.id}`} onClick={() => onTab(t.id)}>
          {t.label}
        </button>
      ))}
      {bar && <span aria-hidden className="tab-underline" style={{ left: bar.left, width: bar.width }} data-testid="tab-underline" />}
    </div>
  );
}

export function TopNav({ tab, onTab, demo, demoHref, liveHref, account }: {
  tab: TabId; onTab: (tab: TabId) => void; demo: boolean; demoHref: string; liveHref: string; account?: ReactNode;
}) {
  return (
    <header className="topnav">
      <div className="mx-auto flex max-w-[1920px] flex-wrap items-center gap-x-8 gap-y-1 px-4 sm:px-8">
        <Wordmark />
        <div className="order-3 w-full min-w-0 lg:order-none lg:w-auto lg:flex-1">
          <TabBar tab={tab} onTab={onTab} />
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2.5 py-2">
          {account}
          {demo
            ? <Link className="btn btn-ghost" href={liveHref}>⏻ Live data</Link>
            : <Link className="btn btn-ghost" href={demoHref}>▶ Demo data</Link>}
        </div>
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
        <div className="mt-3 rounded-lg border border-dashed border-line bg-card-2 p-4 text-sm text-muted">
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
