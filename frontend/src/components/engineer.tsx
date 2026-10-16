"use client";
/**
 * The AI race engineer: the full report (AI Engineer tab), the report card the dock reuses per tab,
 * and the chat (in the dock). Everything shown was validated server-side before it was sent
 * (non-streaming), and every bullet exposes the facts it cites.
 */
import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";

import { ApiError } from "@/lib/api/client";
import type { EngineerAnswer, EngineerAskRequest, EngineerBullet, EngineerReport, EngineerReportRequest,
  FactView } from "@/lib/api/endpoints";
import {
  FOOTER_NOTE, MAX_QUESTION, REGENERATE_COOLDOWN_S, SECTIONS, badgeFor, defaultSuggestions, factValue, factsFor,
  fallbackExplanation, isNeutral, reportText, segmentFor, type SectionKey,
} from "@/lib/engineer";
import { useAskEngineer, useEngineerReport, useRegenerateReport } from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import { useGoToTab } from "@/lib/tab-nav";
import type { TabId } from "@/lib/tabs";


type LapSel = EngineerReportRequest["reference"];

export function engineerRequest(session: string, reference: LapSel, target: LapSel, sessionCode: string | null): EngineerReportRequest {
  return { session_key: session, reference, target, ...(sessionCode === "R" ? { tire_context: {} } : {}) };
}

/** AI Engineer tab: the complete validated report, the chat, and every fact the report rests on. */
export function EngineerSection({ session, reference, target, sessionCode }: {
  session: string; reference: LapSel; target: LapSel; sessionCode: string | null;
}) {
  const request = useMemo(() => engineerRequest(session, reference, target, sessionCode),
    [session, reference, target, sessionCode]);
  return (
    <section id="engineer" className="card" aria-labelledby="engineer-title">
      <div className="kick">✦ AI engineer</div>
      <h2 id="engineer-title" className="display mt-1 text-[22px]">Race engineer report</h2>
      <div className="mt-4 grid gap-6 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <ReportCard request={request} jumpTo="track" />
        {/* keyed by the comparison: a different comparison starts a new conversation */}
        <div className="min-w-0 rounded-lg border border-line bg-card-2 p-4">
          <EngineerChat key={JSON.stringify(request)} request={request} target={target.driver} sessionCode={sessionCode}
            logClassName="max-h-[520px]" />
        </div>
      </div>
      <div className="mt-6"><FactsTable request={request} /></div>
      <p className="mt-4 border-t border-line pt-3 text-xs text-muted" data-testid="engineer-footer">{FOOTER_NOTE}</p>
    </section>
  );
}

/** Every fact the report cites, with its value and meaning: the audit trail behind the text. */
function FactsTable({ request }: { request: EngineerReportRequest }) {
  const report = useEngineerReport(request);
  if (!report.data) return null;
  const facts = Object.values(report.data.facts);
  return (
    <div className="min-w-0 rounded-lg border border-line bg-card-2 p-4" data-testid="engineer-facts">
      <div className="kick">Facts behind the report</div>
      <p className="mt-1 text-xs text-muted">{facts.length} verified numbers from the analysis engine; every figure in the text is one of these.</p>
      <div className="mt-2 max-h-[560px] overflow-y-auto">
        <table className="data-table">
          <thead><tr><th scope="col">Fact</th><th scope="col">Value</th></tr></thead>
          <tbody>
            {facts.map((f) => (
              <tr key={f.id}>
                <td><span className="font-mono text-[11px] text-ink">{f.id}</span><span className="block text-[11px] text-muted">{f.meaning}</span></td>
                <td className="font-semibold whitespace-nowrap">{factValue(f)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- facts
function FactPopover({ bullet, facts }: { bullet: EngineerBullet; facts: Record<string, FactView> }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const shown = factsFor(bullet, facts);
  if (!shown.length) return null;
  return (
    <span className="relative inline-block align-middle" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full border border-line px-1 text-[10px] text-muted hover:text-ink focus:text-ink"
        aria-label={`Supporting facts (${shown.length})`} aria-expanded={open} aria-controls={id}
        onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }} data-testid="fact-indicator">
        {shown.length}
      </button>
      {open && (
        <span id={id} role="tooltip" data-testid="fact-popover"
          className="absolute left-0 top-5 z-30 block w-[min(340px,80vw)] rounded-xl border border-line bg-card p-3 text-left text-xs shadow-lg">
          <span className="kick mb-1 block">Supporting facts</span>
          {shown.map((f) => (
            <span key={f.id} className="mb-1.5 block last:mb-0">
              <span className="font-mono text-[11px] text-ink">{f.id}</span>
              <span className="float-right font-semibold text-ink">{factValue(f)}</span>
              <span className="block text-muted">{f.meaning}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}

export function BulletLine({ bullet, data, jumpTo }: {
  bullet: EngineerBullet; data: Pick<EngineerReport, "facts" | "segments">; jumpTo?: TabId;
}) {
  const selectCorner = useUiStore((s) => s.selectCorner);
  const selected = useUiStore((s) => s.selectedCornerId);
  const goTo = useGoToTab();
  const segment = segmentFor(bullet, data.segments);
  return (
    <li className="leading-relaxed" data-testid="engineer-bullet">
      {segment ? (
        <button type="button" className={`inline text-left hover:underline ${selected === segment ? "text-accent" : ""}`}
          onClick={() => { selectCorner(segment); if (jumpTo) goTo(jumpTo); }} aria-pressed={selected === segment}
          title={jumpTo ? "Show this corner on the track map" : "Highlight on the track map and charts"}>
          {bullet.text}
        </button>
      ) : <span>{bullet.text}</span>}
      <FactPopover bullet={bullet} facts={data.facts} />
    </li>
  );
}

function Badge({ generatedBy, outcome, reason }: { generatedBy: string; outcome: string; reason?: string | null }) {
  const badge = badgeFor(generatedBy);
  const why = fallbackExplanation(outcome, reason);
  const id = useId();
  const cls = badge.kind === "ai" ? "bg-purple-soft text-purple" : "bg-card-2 text-muted";
  return (
    <span className={`chip ${cls}`} data-testid="engineer-badge" title={why ?? undefined} aria-describedby={why ? id : undefined}>
      {badge.kind === "ai" ? "✦ " : ""}{badge.label}
      {why && <span id={id} className="sr-only">{why}</span>}
    </span>
  );
}

// ---------------------------------------------------------------- report card
export function ReportCard({ request, sections, cornersOnly = false, jumpTo, testId = "engineer-report", limitations = true }: {
  request: EngineerReportRequest; sections?: readonly SectionKey[]; cornersOnly?: boolean; jumpTo?: TabId;
  testId?: string; limitations?: boolean;
}) {
  const report = useEngineerReport(request);
  const regenerate = useRegenerateReport(request);
  const [cooldown, setCooldown] = useState(0);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  if (report.isPending) {
    return (
      <div role="status" aria-label="Writing the engineer report" data-testid="engineer-loading">
        <div className="skeleton h-5 w-3/4" /><div className="skeleton mt-3 h-24" />
        <p className="mt-2 text-xs text-muted">Writing the report and checking every number against the telemetry…</p>
      </div>
    );
  }
  if (report.isError) return <ErrorBox error={report.error} title="Couldn't write the engineer report" onRetry={() => report.refetch()} />;
  const r = report.data;
  const why = fallbackExplanation(r.outcome, r.fallback_reason);
  const keys: readonly SectionKey[] = sections ?? SECTIONS.map(([k]) => k);
  const shown = SECTIONS.filter(([k]) => keys.includes(k))
    .map(([k, title]) => [k, title, r.report[k].filter((b) => !cornersOnly || segmentFor(b, r.segments) !== null)] as const)
    .filter(([, , bullets]) => bullets.length > 0);

  async function copy() {
    try {
      await navigator.clipboard.writeText(reportText(r));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard blocked */ }
  }

  return (
    <article aria-labelledby={`${testId}-headline`} data-testid={testId}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge generatedBy={r.generated_by} outcome={r.outcome} reason={r.fallback_reason} />
        <span className="ml-auto flex gap-2">
          <button type="button" className="btn btn-ghost px-2.5 py-1.5 text-[11px]" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
          <button type="button" className="btn btn-ghost px-2.5 py-1.5 text-[11px]" disabled={cooldown > 0 || regenerate.isPending}
            onClick={() => { setCooldown(REGENERATE_COOLDOWN_S); regenerate.mutate(); }} data-testid="engineer-regenerate">
            {regenerate.isPending ? "Regenerating…" : cooldown > 0 ? `Regenerate (${cooldown}s)` : "Regenerate"}
          </button>
        </span>
      </div>
      {why && <p className="mb-2 text-xs text-muted" data-testid="engineer-fallback-note">{why}</p>}
      <div id={`${testId}-headline`} className="text-[16px] font-semibold" role="heading" aria-level={3}>
        <ul><BulletLine bullet={r.report.headline} data={r} jumpTo={jumpTo} /></ul>
      </div>
      {shown.map(([key, title, bullets]) => (
        <div key={key} className="mt-4">
          <div className="kick">{title}</div>
          <ul className="mt-1.5 list-disc space-y-1.5 pl-5 text-[14px] marker:text-accent">
            {bullets.map((b, i) => <BulletLine key={i} bullet={b} data={r} jumpTo={jumpTo} />)}
          </ul>
        </div>
      ))}
      {shown.length === 0 && <p className="mt-3 text-sm text-muted" data-testid={`${testId}-empty`}>The report has nothing specific to this view.</p>}
      {limitations && <div className="mt-4 rounded-lg border border-line bg-card-2 p-3" data-testid="engineer-limitations">
        <div className="kick">Data limitations</div>
        {r.report.data_limitations.length ? (
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
            {r.report.data_limitations.map((b, i) => <BulletLine key={i} bullet={b} data={r} />)}
          </ul>
        ) : <p className="mt-1 text-sm text-muted">No additional limitations were reported for this comparison.</p>}
      </div>}
    </article>
  );
}

function ErrorBox({ error, title, onRetry }: { error: Error; title: string; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="rounded-xl border border-red bg-red-soft p-3" role="alert" data-testid="engineer-error">
      <div className="text-sm font-bold text-red-ink">⚠ {title}</div>
      <p className="mt-1 text-sm text-ink-2">{error.message}</p>
      {api && (
        <p className="mt-2 flex flex-wrap gap-1.5 text-xs">
          <span className="chip">code: {api.code}</span>
          {api.requestId && <span className="chip">request_id: {api.requestId}</span>}
        </p>
      )}
      {onRetry && <button type="button" className="btn btn-ghost mt-2 text-xs" onClick={onRetry}>Retry</button>}
    </div>
  );
}

// ---------------------------------------------------------------- chat
interface Turn { question: string; answer?: EngineerAnswer; error?: Error }

export function EngineerChat({ request, target, sessionCode, suggestions, logClassName = "max-h-[420px]" }: {
  request: EngineerReportRequest; target: string; sessionCode: string | null; suggestions?: string[]; logClassName?: string;
}) {
  const ask = useAskEngineer();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const conversation = useRef<string | null>(null);
  const inputId = useId();


  const last = [...turns].reverse().find((t) => t.answer)?.answer;
  const chips = last?.follow_up_suggestions.length
    ? last.follow_up_suggestions.map((f) => f.question)
    : suggestions ?? defaultSuggestions(target, sessionCode);

  function send(question: string) {
    const q = question.trim();
    if (!q || ask.isPending) return;
    setText("");
    setTurns((t) => [...t, { question: q }]);
    const body: EngineerAskRequest = { session_key: request.session_key, reference: request.reference,
      target: request.target, question: q, ...(conversation.current ? { conversation_id: conversation.current } : {}),
      ...(request.tire_context ? { tire_context: request.tire_context } : {}) };
    ask.mutate(body, {
      onSuccess: (answer) => {
        conversation.current = answer.conversation_id;
        setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, answer } : x)));
      },
      onError: (error) => setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error } : x))),
    });
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    send(text);
  }

  return (
    <div className="flex min-w-0 flex-col" data-testid="engineer-chat">
      <div className="kick">Ask the engineer</div>
      <div className={`mt-2 flex ${logClassName} flex-col gap-2 overflow-y-auto`} aria-live="polite" aria-relevant="additions"
        role="log" aria-label="Engineer answers" data-testid="engineer-log">
        {turns.length === 0 && <p className="text-sm text-muted">Ask about this comparison, or pick a suggestion.</p>}
        {turns.map((t, i) => (
          <div key={i} className="flex flex-col gap-1.5">
            <div className="self-end rounded-xl bg-[var(--nav-active)] px-3 py-1.5 text-sm text-[var(--nav-active-ink)]">{t.question}</div>
            {t.answer && <AnswerBubble answer={t.answer} />}
            {t.error && <ErrorBox error={t.error} title="The engineer couldn't answer" onRetry={() => send(t.question)} />}
            {!t.answer && !t.error && (
              <div className="self-start text-xs text-muted" role="status" data-testid="engineer-thinking">Engineer is thinking…</div>
            )}
          </div>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5" aria-label="Suggested questions" role="group">
        {chips.map((c) => (
          <button key={c} type="button" className="chip hover:text-ink" onClick={() => send(c)} disabled={ask.isPending}
            data-testid="engineer-chip">{c}</button>
        ))}
      </div>
      <form className="mt-2 flex flex-wrap gap-2" onSubmit={submit}>
        <label htmlFor={inputId} className="sr-only">Question for the engineer</label>
        <input id={inputId} className="min-w-0 flex-1 rounded-lg border border-line bg-card px-3 py-2 text-sm"
          value={text} maxLength={MAX_QUESTION} onChange={(e) => setText(e.target.value)}
          placeholder="e.g. What happened in Turn 6?" aria-describedby={`${inputId}-count`} data-testid="engineer-input" />
        <button type="submit" className="btn px-3" disabled={!text.trim() || ask.isPending}>Ask</button>
      </form>
      <div id={`${inputId}-count`} className="mt-1 text-right text-[11px] text-muted">{text.length}/{MAX_QUESTION}</div>
    </div>
  );
}

function AnswerBubble({ answer }: { answer: EngineerAnswer }) {
  const neutral = isNeutral(answer);
  return (
    <div className={`self-start rounded-xl border px-3 py-2 text-sm ${neutral ? "border-line bg-card text-ink-2" : "border-line bg-card"}`}
      data-testid="engineer-answer" data-variant={neutral ? "neutral" : "answer"}>
      {!neutral && <div className="mb-1"><Badge generatedBy={answer.generated_by} outcome={answer.outcome} reason={answer.fallback_reason} /></div>}
      <ul><BulletLine bullet={answer.answer} data={answer} /></ul>
    </div>
  );
}
