/**
 * Test harness: an in-memory URL behind next/navigation (so URL-state logic is really exercised)
 * plus the real Providers/Dashboard. Plotly is stubbed (jsdom has no layout engine); the chart is
 * covered by the Playwright e2e test instead.
 */
import { render } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import { vi } from "vitest";

let search = "";
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export const navigation = {
  setSearch(next: string) {
    search = next.replace(/^\?/, "");
    emit();
  },
  get search() {
    return search;
  },
};

export const nextNavigationMock = {
  useSearchParams: () => {
    const value = useSyncExternalStore((l) => (listeners.add(l), () => listeners.delete(l)), () => search, () => search);
    return new URLSearchParams(value);
  },
  usePathname: () => "/",
  useRouter: () => ({
    replace: (href: string) => {
      const q = href.includes("?") ? href.slice(href.indexOf("?") + 1) : "";
      window.history.replaceState(null, "", `/?${q}`);
      navigation.setSearch(q);
    },
    push: vi.fn(),
    refresh: vi.fn(),
  }),
};

export async function renderDashboard(url: string) {
  navigation.setSearch(url);
  window.history.replaceState(null, "", `/?${navigation.search}`);
  const { DashboardPage } = await import("@/app/dashboard-page");
  return render(<DashboardPage />);
}
