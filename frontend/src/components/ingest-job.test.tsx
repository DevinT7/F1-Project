import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import session from "@/fixtures/api/session_2024-05-Q.json";
import { useUiStore } from "@/lib/store";
import { server } from "@/test/msw";

vi.mock("next/navigation", async () => (await import("@/test/render")).nextNavigationMock);
const { renderDashboard } = await import("@/test/render");

const JOB = {
  id: "a".repeat(32), kind: "ingest", target: "2024-06-Q", status: "queued", progress: 0, message: null, error: null,
  attempts: 0, max_attempts: 3, run_after: null, result: null, created_at: "2026-10-14T00:00:00Z",
  updated_at: "2026-10-14T00:00:00Z", status_url: `/api/v1/jobs/${"a".repeat(32)}`,
};

beforeEach(() => useUiStore.setState({ toasts: [] }));

describe("ingest as a background job", () => {
  it("POST → 202 job, then polls progress until the session is ready", async () => {
    const user = userEvent.setup();
    let polls = 0;
    let stored = false;
    server.use(
      http.get("*/api/v1/sessions/:key", ({ params }) => String(params.key) === "2024-06-Q" && stored
        ? HttpResponse.json({ ...session, session_key: "2024-06-Q" })
        : HttpResponse.json({ session_key: params.key, status: "not_ingested", info: null, manifest: null })),
      http.post("*/api/v1/sessions/:key/ingest", () => HttpResponse.json(JOB, { status: 202 })),
      http.get("*/api/v1/jobs/:id", () => {
        polls += 1;
        if (polls < 3) return HttpResponse.json({ ...JOB, status: "running", progress: 0.4, message: "telemetry 200/500 laps" });
        stored = true;
        return HttpResponse.json({ ...JOB, status: "succeeded", progress: 1, message: "done",
          result: { session_key: "2024-06-Q", skipped: false, laps: 279 } });
      }),
    );
    await renderDashboard("s=2024-06-Q");
    await user.click(await screen.findByRole("button", { name: "Ingest session" }));
    expect(await screen.findByTestId("ingest-progress")).toHaveTextContent("40% · telemetry 200/500 laps");
    expect(screen.getByRole("progressbar", { name: "Ingest progress" })).toHaveAttribute("aria-valuenow", "40");
    await waitFor(() => expect(screen.getByText(/laps stored/)).toBeInTheDocument(), { timeout: 8000 });
  });

  it("a failed job shows its error and offers a retry", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/v1/sessions/:key", ({ params }) =>
        HttpResponse.json({ session_key: params.key, status: "not_ingested", info: null, manifest: null })),
      http.post("*/api/v1/sessions/:key/ingest", () => HttpResponse.json(JOB, { status: 202 })),
      http.get("*/api/v1/jobs/:id", () => HttpResponse.json({ ...JOB, status: "failed", attempts: 1,
        error: "PermanentJobError: FastF1 could not load 2024 round 6 Q" })),
    );
    await renderDashboard("s=2024-06-Q");
    await user.click(await screen.findByRole("button", { name: "Ingest session" }));
    expect(await screen.findByTestId("ingest-failed")).toHaveTextContent("after 1 attempt: PermanentJobError");
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });
});
