import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import comparisonJson from "@/fixtures/api/comparison_VER_ALO.json";
import cornersJson from "@/fixtures/api/corners.json";
import type { ComparisonResponse, CornersResponse } from "@/lib/api/endpoints";
import { buildReplay, jumpToCorner } from "@/lib/replay";
import { useUiStore } from "@/lib/store";

import { TrackMap } from "./track-map";

const comparison = comparisonJson as unknown as ComparisonResponse;
const corners = cornersJson as unknown as CornersResponse;
const dist = (side: "reference" | "target") => Number(screen.getByTestId(`replay-dot-${side}`).getAttribute("data-distance"));

beforeEach(() => {
  useUiStore.setState({ selectedCornerId: null, hoverDistance: null });
});

describe("track map views", () => {
  it("opens on the time gained / lost view with its legend", () => {
    render(<TrackMap comparison={comparison} corners={corners} />);
    expect(screen.getByTestId("map-mode-delta")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Map legend")).toHaveTextContent("Target gaining");
    expect(screen.queryByTestId("replay")).toBeNull();
  });

  it("lap replay: both team-coloured dots start on the start/finish line", async () => {
    const user = userEvent.setup();
    render(<TrackMap comparison={comparison} corners={corners} />);
    await user.click(screen.getByTestId("map-mode-replay"));
    expect(screen.getByTestId("start-finish")).toBeInTheDocument();
    expect(dist("reference")).toBe(0);
    expect(dist("target")).toBe(0);
    expect(screen.getByTestId("replay-dot-reference").textContent).toBe("VER");
    expect(screen.getByTestId("replay-dot-target").textContent).toBe("ALO");
    expect(screen.getByTestId("replay-status")).toHaveTextContent("start from the line");
    // corner numbers stay on
    expect(screen.getByTestId("map-label-T6")).toBeInTheDocument();
  });

  it("scrubbing moves the drivers at their real pace and shows who leads", async () => {
    const user = userEvent.setup();
    render(<TrackMap comparison={comparison} corners={corners} />);
    await user.click(screen.getByTestId("map-mode-replay"));
    fireEvent.change(screen.getByTestId("replay-scrub"), { target: { value: "60" } });
    expect(dist("reference")).toBeGreaterThan(dist("target"));          // VER is faster over the lap
    expect(screen.getByTestId("replay-status")).toHaveTextContent(/VER leads by \d\.\d{3} s \(\d+ m\)/);
    expect(screen.getByTestId("replay-gap")).toBeInTheDocument();
  });

  it("clicking a corner number moves the faster driver there and the slower one to where it was then", async () => {
    render(<TrackMap comparison={comparison} corners={corners} />);
    fireEvent.click(screen.getByTestId("map-mode-replay"));
    const t6 = comparison.findings.corners.find((f) => f.corner_number === 6)!;
    const apex = t6.segment!.apex_m ?? t6.segment!.start_m;
    const replay = buildReplay(comparison.distance_m, comparison.channels.reference.time_s, comparison.channels.target.time_s)!;
    const expected = jumpToCorner(replay, apex);

    fireEvent.click(screen.getByTestId("map-label-T6"));
    await waitFor(() => expect(dist(expected.faster)).toBeCloseTo(apex, 0), { timeout: 3000 });
    expect(dist(expected.slower)).toBeCloseTo(expected.slowerDistance, 0);
    expect(dist(expected.slower)).toBeLessThan(apex);
    expect(useUiStore.getState().selectedCornerId).toBe(t6.id);
    const code = { reference: "VER", target: "ALO" } as const;
    expect(screen.getByTestId("replay-status")).toHaveTextContent(
      `Turn 6: ${code[expected.faster]} gets there first; ${code[expected.slower]} is`);
  });

  it("the corner chips jump too, and play / start controls work", async () => {
    const user = userEvent.setup();
    render(<TrackMap comparison={comparison} corners={corners} />);
    await user.click(screen.getByTestId("map-mode-replay"));
    await user.click(screen.getByTestId("corner-T16"));
    await waitFor(() => expect(screen.getByTestId("replay-status")).toHaveTextContent(/^Turn 16:/), { timeout: 3000 });
    await user.click(screen.getByTestId("replay-reset"));
    await user.click(screen.getByTestId("replay-play"));
    expect(screen.getByTestId("replay-play")).toHaveTextContent("Pause");
    await waitFor(() => expect(dist("reference")).toBeGreaterThan(0));
    await user.click(screen.getByTestId("replay-play"));
    await user.click(screen.getByTestId("replay-reset"));
    expect(dist("reference")).toBe(0);
  });
});
