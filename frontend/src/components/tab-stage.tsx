"use client";
/**
 * Tab transitions: a broadcast-style replay wipe (a red band with a chequered leading edge) sweeps
 * across the panel while the new tab "launches" in behind it. The tab itself switches at once (the
 * wipe is decoration only), and prefers-reduced-motion turns all of it off (globals.css).
 */
import { useEffect, useRef, useState, type ReactNode } from "react";

import type { TabId } from "@/lib/tabs";

export function TabStage({ tab, children }: { tab: TabId; children: ReactNode }) {
  const first = useRef(true);
  const [wipe, setWipe] = useState(0);

  useEffect(() => {
    if (first.current) { first.current = false; return; }
    setWipe((w) => w + 1);
  }, [tab]);

  return (
    <div className="relative" role="tabpanel" id="tab-panel" aria-labelledby={`tab-${tab}`} data-testid="tab-panel" data-tab={tab}>
      {wipe > 0 && (
        <div key={wipe} className="pit-wipe" aria-hidden data-testid="tab-wipe"
          onAnimationEnd={(e) => { if (e.target === e.currentTarget.firstElementChild) setWipe(0); }}>
          <i />
        </div>
      )}
      <div key={tab} className="tab-enter">{children}</div>
    </div>
  );
}
