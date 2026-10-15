import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useUiStore } from "@/lib/store";
import { signInAs, signOutForTest } from "@/test/auth";
import { apiError, server } from "@/test/msw";
import { navigation } from "@/test/render";

vi.mock("next/navigation", async () => (await import("@/test/render")).nextNavigationMock);
vi.mock("@/components/telemetry-stack", () => ({
  TelemetryStack: () => <section id="telemetry" data-testid="telemetry-plot" />,
}));

const { renderDashboard } = await import("@/test/render");
const SHARED = "s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest";

beforeEach(() => {
  useUiStore.setState({ toasts: [], selectedCornerId: null, hoverDistance: null });
});

describe("SessionSelector cascade", () => {
  it("season → Grand Prix → session writes a shareable URL and preselects the two fastest laps", async () => {
    const user = userEvent.setup();
    await renderDashboard("");

    const gp = screen.getByLabelText("Grand Prix");
    expect(gp).toBeDisabled();
    expect(screen.getByLabelText("Session")).toBeDisabled();

    await waitFor(() => expect(screen.getByLabelText("Season")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Season"), "2024");
    await waitFor(() => expect(screen.getByLabelText("Grand Prix")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Grand Prix"), "5");
    expect(screen.getByRole("option", { name: "R05 · Chinese Grand Prix" })).toBeInTheDocument();

    const session = screen.getByLabelText("Session");
    await waitFor(() => expect(session).toBeEnabled());
    await user.selectOptions(session, "Q");

    expect(navigation.search).toMatch(/^s=2024-05-Q/);
    expect(await screen.findByText(/279 laps stored|9 laps stored/)).toBeInTheDocument();
    // no laps chosen yet → the two fastest drivers' fastest laps, written back to the URL
    await waitFor(() => expect(navigation.search).toBe("s=2024-05-Q&ref=VER:fastest&tgt=PER:fastest"));
    expect(screen.getByLabelText("Reference driver")).toHaveValue("VER");
    expect(screen.getByLabelText("Target driver")).toHaveValue("PER");
  });

  it("changing the season clears the downstream choices", async () => {
    const user = userEvent.setup();
    await renderDashboard(SHARED);
    await waitFor(() => expect(screen.getByLabelText("Session")).toHaveValue("Q"));
    await waitFor(() => expect(screen.getByLabelText("Season")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Season"), "2023");
    expect(navigation.search).toBe("");
    expect(screen.getByLabelText("Session")).toBeDisabled();
  });
});

describe("loading a shared URL", () => {
  it("reproduces the comparison from the URL alone", async () => {
    await renderDashboard(SHARED);
    expect(await screen.findByTestId("lap-gap")).toHaveTextContent("+0.488 s");
    expect(screen.getByLabelText("Reference lap")).toHaveValue("fastest");
    expect(screen.getByText("Where ALO lost and gained time")).toBeInTheDocument();
    expect(screen.getByTestId("race-brief")).toHaveTextContent("ALO was 0.488 s slower than VER");
    expect(screen.getByTestId("site-notice")).toHaveTextContent(/unofficial.*not associated in any way with the Formula 1 companies/);
    expect(screen.getByTestId("site-notice")).toHaveTextContent("FastF1");
  });

  it("flags an invalid lap in the picker", async () => {
    await renderDashboard("s=2024-05-Q&ref=VER:fastest&tgt=ALO:1");
    const target = await screen.findByTestId("pick-target");
    expect(await within(target).findByText(/invalid: no lap time/)).toBeInTheDocument();
  });
});

describe("API errors", () => {
  it("show a toast with the error code and request id", async () => {
    server.use(http.post("*/api/v1/comparisons", () =>
      apiError(422, "lap_invalid", "ALO lap 1 is not valid for comparison (no_lap_time).", "req-abc-123")));
    await renderDashboard(SHARED);

    const toast = await screen.findByTestId("toast");
    expect(toast).toHaveTextContent("ALO lap 1 is not valid for comparison");
    expect(toast).toHaveTextContent("code: lap_invalid");
    expect(toast).toHaveTextContent("request_id: req-abc-123");
    // and the comparison area explains it in place
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't compare these laps");
  });

  it("explain an unreachable backend", async () => {
    server.use(http.get("*/api/v1/seasons", () => Response.error()));
    await renderDashboard("");
    // after the single network retry
    expect(await screen.findByTestId("toast", {}, { timeout: 5000 })).toHaveTextContent("code: network_error");
  });
});

describe("corner selection", () => {
  it("defaults to the largest loss and a corner click updates the panel", async () => {
    const user = userEvent.setup();
    await renderDashboard(SHARED);
    const panel = await screen.findByTestId("corner-panel");
    expect(within(panel).getByRole("heading", { level: 3 })).toHaveTextContent("Turn 16");

    await user.click(screen.getByTestId("corner-T6"));
    expect(within(panel).getByRole("heading", { level: 3 })).toHaveTextContent("Turn 6");
    const entry = within(panel).getByRole("row", { name: /Entry speed/ });
    expect(entry).toHaveTextContent("282.0 km/h");
    expect(entry).toHaveTextContent("264.9 km/h");
    expect(entry).toHaveTextContent("−17.1 km/h");
    expect(within(panel).getByRole("row", { name: /Braking point/ })).toHaveTextContent("+5 m");
    expect(within(panel).getByTestId("corner-delta")).toHaveTextContent("+0.098 s (slower)");
    expect(screen.getByTestId("corner-T6")).toHaveAttribute("aria-pressed", "true");
  });

  it("clicking a finding selects that corner", async () => {
    const user = userEvent.setup();
    await renderDashboard(SHARED);
    const findings = await screen.findByTestId("findings-list");
    await user.click(within(findings).getByRole("button", { name: /Turn 11/ }));
    expect(within(screen.getByTestId("corner-panel")).getByRole("heading", { level: 3 })).toHaveTextContent("Turn 11");
  });

  it("map colours agree with the findings' signs", async () => {
    await renderDashboard(SHARED);
    await screen.findByTestId("corner-panel");
    for (const [corner, glyph] of [["T16", "▲"], ["T11", "▼"], ["T8", "●"]] as const) {
      expect(screen.getByTestId(`corner-${corner}`)).toHaveTextContent(glyph);
    }
  });
});

describe("demo mode", () => {
  it("renders the comparison with the network blocked", async () => {
    server.use(http.all("*", () => { throw new Error("demo mode must not call the API"); }));
    await renderDashboard(`${SHARED}&demo=1`);
    expect(await screen.findByTestId("lap-gap")).toHaveTextContent("+0.488 s");
    expect(screen.getByText(/Demo data: real API output/)).toBeInTheDocument();
  });

  it("shows the tyre section's qualifying view without errors (no backend, no toast)", async () => {
    server.use(http.all("*", () => { throw new Error("demo mode must not call the API"); }));
    await renderDashboard(`${SHARED}&tab=tires&demo=1`);
    expect(await screen.findByTestId("tire-state-non-race")).toBeInTheDocument();
    expect(await screen.findByTestId("stint-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("qualifying-tire-chart")).toHaveTextContent("Observed laps — not a prediction");
    expect(useUiStore.getState().toasts).toEqual([]);
  });
});

describe("tabs", () => {
  it("shows only the five tabs, as words, with the selected one marked; no theme switch", async () => {
    await renderDashboard(SHARED);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Pit Wall", "Lap Comparison", "Track Map", "Tire Analysis", "AI Engineer"]);
    expect(screen.getByRole("tab", { name: "Pit Wall" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: /theme/i })).toBeNull();
    expect(screen.queryByText("None selected")).toBeNull();          // the old sidebar's session box is gone
    expect(screen.getByLabelText("Senna")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/RaceIQ|RACEIQ/);
  });

  it("switching tabs swaps the panel and writes the tab to the URL", async () => {
    const user = userEvent.setup();
    await renderDashboard(SHARED);
    await screen.findByTestId("race-brief");
    await user.click(screen.getByRole("tab", { name: "Lap Comparison" }));
    expect(navigation.search).toBe(`${SHARED}&tab=laps`);
    expect(await screen.findByTestId("telemetry-plot")).toBeInTheDocument();
    expect(screen.getByTestId("straights-table")).toHaveTextContent("Straight 1");
    expect(screen.queryByTestId("race-brief")).toBeNull();
    expect(screen.getByRole("tab", { name: "Lap Comparison" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Lap Comparison");

    await user.click(screen.getByRole("tab", { name: "Track Map" }));
    expect(await screen.findByTestId("corner-table")).toHaveTextContent("All 16 corners");
    await user.click(screen.getByTestId("corner-row-6"));
    expect(within(screen.getByTestId("corner-panel")).getByRole("heading", { level: 3 })).toHaveTextContent("Turn 6");
  });

  it("arrow keys move between tabs", async () => {
    const user = userEvent.setup();
    await renderDashboard(`${SHARED}&tab=track`);
    await screen.findByTestId("corner-table");
    screen.getByRole("tab", { name: "Track Map" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(navigation.search).toContain("tab=tires");
    expect(screen.getByRole("tab", { name: "Tire Analysis" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(navigation.search).toContain("tab=laps");
  });
});

describe("engineer dock", () => {
  it("asks for a sign-in when signed out", async () => {
    signOutForTest();
    await renderDashboard(SHARED);
    expect(await screen.findByTestId("dock-signin")).toHaveTextContent("Sign in to ask the race engineer");
  });

  it("generates the report for the selected tab and suggests questions about it", async () => {
    const user = userEvent.setup();
    signInAs("user");
    await renderDashboard(`${SHARED}&tab=track`);
    const dock = await screen.findByTestId("engineer-dock");
    expect(within(dock).getByTestId("dock-context")).toHaveTextContent("Track Map");
    expect((await within(dock).findAllByTestId("engineer-chip"))[0]).toHaveTextContent("Which corners should ALO focus on?");
    await user.click(within(dock).getByTestId("dock-generate"));
    const report = await within(dock).findByTestId("dock-report");
    // the corner report keeps only bullets that point at a corner, straight or sector
    for (const b of within(report).getAllByTestId("engineer-bullet").slice(1)) {
      expect(within(b).getByTitle(/track map/)).toBeInTheDocument();
    }
    await user.click(screen.getByRole("tab", { name: "Lap Comparison" }));
    expect(within(dock).getByTestId("dock-context")).toHaveTextContent("Lap Comparison");
    expect(within(dock).getByTestId("dock-generate")).toBeInTheDocument();   // a report per tab, on request
    expect(within(dock).getAllByTestId("engineer-chip")[1]).toHaveTextContent("Is ALO slower on the straights or in the corners?");
  });

  it("a report bullet on the AI Engineer tab opens its corner on the Track Map tab", async () => {
    const user = userEvent.setup();
    signInAs("user");
    await renderDashboard(`${SHARED}&tab=engineer`);
    const report = await screen.findByTestId("engineer-report");
    expect(screen.getByTestId("engineer-facts")).toHaveTextContent("LAP.time_loss_s");
    await user.click(within(report).getByRole("button", { name: /^Turn 16: ALO lost 0\.176 s/ }));
    expect(navigation.search).toContain("tab=track");
    expect(within(await screen.findByTestId("corner-panel")).getByRole("heading", { level: 3 })).toHaveTextContent("Turn 16");
  });
});
