"use client";

import { useMemo } from "react";

import type { ComparisonResponse } from "./api/endpoints";
import { ensureContrast, sameTeamColor } from "./colors";
import { useUiStore } from "./store";

export interface ThemePalette {
  card: string;
  ink: string;
  muted: string;
  line: string;
  green: string;
  red: string;
  accent: string;
  neutral: string;
}

const FALLBACK: ThemePalette = {
  card: "#1b1a19", ink: "#f1eeeb", muted: "#aaa49f", line: "#2f2c2a",
  green: "#4cb582", red: "#e36a55", accent: "#e2775a", neutral: "#8f8a86",
};

function read(): ThemePalette {
  if (typeof window === "undefined") return FALLBACK;
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fb: string) => css.getPropertyValue(name).trim() || fb;
  return {
    card: v("--card", FALLBACK.card), ink: v("--ink", FALLBACK.ink), muted: v("--muted", FALLBACK.muted),
    line: v("--line", FALLBACK.line), green: v("--green", FALLBACK.green), red: v("--red", FALLBACK.red),
    accent: v("--accent", FALLBACK.accent), neutral: v("--neutral", FALLBACK.neutral),
  };
}

/** CSS-variable palette for SVG/Plotly, re-read whenever the theme toggles. */
export function useThemePalette(): ThemePalette {
  const theme = useUiStore((s) => s.theme);
  // re-read the CSS variables whenever the theme flips (the DOM attribute is already updated)
  return useMemo(() => (theme ? read() : FALLBACK), [theme]);
}

export interface DriverStyles {
  /** color: ≥ 3:1 for lines; text: ≥ 4.5:1 for small text in that driver's colour */
  reference: { color: string; text: string; dash: "solid" | "dash"; label: string };
  target: { color: string; text: string; dash: "solid" | "dash"; label: string };
}

/**
 * Team colours, contrast-adjusted for the current card background (≥ 3:1). Team-mates share a
 * colour, so the target is drawn dashed to stay distinguishable. Fallbacks are the original
 * dashboard's blue (reference) and terracotta (target).
 */
export function driverStyles(cmp: ComparisonResponse, palette: ThemePalette): DriverStyles {
  const ref = cmp.meta.reference, tgt = cmp.meta.target;
  const same = sameTeamColor(ref.team_color, tgt.team_color);
  return {
    reference: {
      color: ensureContrast(ref.team_color ?? "#3b7dd8", palette.card),
      text: ensureContrast(ref.team_color ?? "#3b7dd8", palette.card, 4.5),
      dash: "solid", label: `${ref.driver} L${ref.lap_number}`,
    },
    target: {
      color: ensureContrast(tgt.team_color ?? "#c1553a", palette.card),
      text: ensureContrast(tgt.team_color ?? "#c1553a", palette.card, 4.5),
      dash: same ? "dash" : "solid", label: `${tgt.driver} L${tgt.lap_number}`,
    },
  };
}
