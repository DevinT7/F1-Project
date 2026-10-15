"use client";
/**
 * The engineer dock (right column, the same width the old sidebar had): it follows the selected tab.
 * "Generate" shows the validated report's sections that tab is about (TAB_FOCUS), and the chat's
 * suggested questions change with the tab. One conversation per comparison, kept across tab switches.
 */
import { useMemo, useState } from "react";

import type { EngineerReportRequest } from "@/lib/api/endpoints";
import { FOOTER_NOTE, TAB_FOCUS, suggestionsFor } from "@/lib/engineer";
import { tabLabel, type TabId } from "@/lib/tabs";

import { EngineerChat, ReportCard, engineerRequest } from "./engineer";

type LapSel = EngineerReportRequest["reference"];

export function EngineerDock({ tab, session, reference, target, sessionCode }: {
  tab: TabId; session: string | null; reference: LapSel | null; target: LapSel | null; sessionCode: string | null;
}) {
  const request = useMemo(
    () => (session && reference && target ? engineerRequest(session, reference, target, sessionCode) : null),
    [session, reference, target, sessionCode],
  );

  return (
    <aside id="engineer-dock" aria-label="AI engineer" data-testid="engineer-dock"
      className="border-t border-line bg-[rgba(10,10,13,0.9)] px-4 py-5 xl:sticky xl:top-[66px] xl:h-[calc(100vh-66px)] xl:overflow-y-auto xl:border-t-0 xl:border-l">
      <div className="kick">✦ AI engineer</div>
      <div className="display mt-1 text-[19px]">Race engineer</div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
        <span className="chip border-accent-strong text-ink" data-testid="dock-context">{tabLabel(tab)}</span>
        {target && reference && <span>{target.driver} vs {reference.driver}</span>}
      </div>

      <div className="mt-4">
        {!request ? (
          <p className="rounded-lg border border-dashed border-line p-3 text-sm text-muted" data-testid="dock-empty">
            Pick a session and two laps, and the engineer will brief you on every tab.
          </p>
        ) : (
          <DockBody key={JSON.stringify(request)} tab={tab} request={request} target={target!.driver} sessionCode={sessionCode} />
        )}
      </div>
      <p className="mt-5 border-t border-line pt-3 text-[11px] text-muted">{FOOTER_NOTE}</p>
    </aside>
  );
}

function DockBody({ tab, request, target, sessionCode }: {
  tab: TabId; request: EngineerReportRequest; target: string; sessionCode: string | null;
}) {
  const [generated, setGenerated] = useState<ReadonlySet<TabId>>(new Set());
  const focus = TAB_FOCUS[tab];
  return (
    <div className="flex flex-col gap-5">
      <section aria-labelledby="dock-report-title" className="rounded-lg border border-line bg-card p-3">
        <div id="dock-report-title" className="text-[13px] font-bold tracking-wide uppercase">{focus.title}</div>
        {tab === "engineer" ? (
          <p className="mt-1 text-xs text-muted">The full report and every fact behind it are open on this tab.</p>
        ) : generated.has(tab) ? (
          <div className="mt-2 text-[13px]">
            <ReportCard request={request} sections={focus.sections} cornersOnly={focus.cornersOnly} testId="dock-report" limitations={false} />
          </div>
        ) : (
          <>
            <p className="mt-1 text-xs text-muted">A report on what this tab shows, written from the verified findings.</p>
            <button type="button" className="btn mt-2 w-full justify-center" data-testid="dock-generate"
              onClick={() => setGenerated((g) => new Set(g).add(tab))}>
              Generate report
            </button>
          </>
        )}
      </section>
      <EngineerChat request={request} target={target} sessionCode={sessionCode}
        suggestions={suggestionsFor(tab, target, sessionCode)} logClassName="max-h-[46vh]" />
    </div>
  );
}
