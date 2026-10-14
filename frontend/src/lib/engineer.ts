/** Phase 7F engineer helpers: section titles, badges, fallback explanations, segment lookup, copy text. */
import type { EngineerAnswer, EngineerBullet, EngineerReport, FactView } from "./api/endpoints";

export const SECTIONS = [
  ["biggest_time_loss", "Biggest time loss"],
  ["strongest_areas", "Strongest areas"],
  ["tire_notes", "Tyres"],
  ["driving_suggestions", "Driving suggestions"],
  ["strategy_options", "Strategy options"],
] as const;

export const FOOTER_NOTE = "Numbers are computed by the analysis engine; the AI only explains them.";
export const MAX_QUESTION = 300;
export const REGENERATE_COOLDOWN_S = 10;

export function badgeFor(generatedBy: string): { label: string; kind: "ai" | "template" | "router" } {
  if (generatedBy === "llm") return { label: "AI-written from verified telemetry", kind: "ai" };
  if (generatedBy === "router") return { label: "Engineer", kind: "router" };
  return { label: "Template summary", kind: "template" };
}

/** Why a template is shown instead of AI text (the badge tooltip). */
export function fallbackExplanation(outcome: string, reason: string | null | undefined): string | null {
  switch (outcome) {
    case "template_disabled":
      return "The AI writer is switched off, so this summary is generated directly from the verified findings.";
    case "template_no_llm":
      return "No AI model is configured, so this summary is generated directly from the verified findings.";
    case "template_validation":
      return `The AI draft failed the fact check${reason ? ` (${reason.replace(/,/g, ", ")})` : ""}, so this ` +
        "deterministic summary replaced it.";
    case "template_provider_error":
      return "The AI service did not respond in time, so this deterministic summary was used instead.";
    default:
      return null;
  }
}

/** The finding id a bullet points at: its first cited corner / straight / sector (not the whole lap). */
export function segmentFor(bullet: EngineerBullet, segments: Record<string, string>): string | null {
  for (const id of bullet.fact_ids) {
    const prefix = id.split(".")[0]!;
    if (prefix !== "LAP" && segments[prefix]) return segments[prefix]!;
  }
  return null;
}

export function factsFor(bullet: EngineerBullet, facts: Record<string, FactView>): FactView[] {
  return bullet.fact_ids.map((id) => facts[id]).filter((f): f is FactView => f !== undefined);
}

export function factValue(f: FactView): string {
  if (f.value === null) return "unavailable";
  if (f.display !== null && f.unit) {
    const unit = ["count", "rank", "corner", "lap", "gear", "fraction"].includes(f.unit) ? "" : ` ${f.unit}`;
    const sign = typeof f.value === "number" && f.value < 0 ? "−" : "";
    return `${sign}${f.display}${unit}`;
  }
  if (Array.isArray(f.value)) return f.value.length ? f.value.join(", ") : "none";
  return String(f.value);
}

export function reportText(r: EngineerReport): string {
  const lines = [r.report.headline.text, ""];
  for (const [key, title] of [...SECTIONS, ["data_limitations", "Data limitations"] as const]) {
    const bullets = r.report[key];
    if (!bullets.length) continue;
    lines.push(`${title}:`, ...bullets.map((b) => `- ${b.text}`), "");
  }
  lines.push(`(${badgeFor(r.generated_by).label} · ${FOOTER_NOTE})`);
  return lines.join("\n");
}

export function isNeutral(a: EngineerAnswer): boolean {
  return a.outcome === "refused" || a.outcome === "clarification";
}

export function defaultSuggestions(target: string, sessionCode: string | null): string[] {
  const out = [`Where did ${target} lose the most time?`, `Which corners should ${target} focus on?`,
    "Was the time loss caused by the driver, the tyres or traffic?"];
  if (sessionCode === "R") out.push(`When should ${target} pit?`);
  return out;
}
