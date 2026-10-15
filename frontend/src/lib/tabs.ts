/** The five top-level tabs. The active one is in the URL (?tab=…; Pit Wall is the default and omitted). */
export const TABS = [
  { id: "pitwall", label: "Pit Wall" },
  { id: "laps", label: "Lap Comparison" },
  { id: "track", label: "Track Map" },
  { id: "tires", label: "Tire Analysis" },
  { id: "engineer", label: "AI Engineer" },
] as const;

export type TabId = (typeof TABS)[number]["id"];
export const DEFAULT_TAB: TabId = "pitwall";

export function parseTab(raw: string | null | undefined): TabId {
  return TABS.find((t) => t.id === raw)?.id ?? DEFAULT_TAB;
}

export function tabLabel(id: TabId): string {
  return TABS.find((t) => t.id === id)!.label;
}
