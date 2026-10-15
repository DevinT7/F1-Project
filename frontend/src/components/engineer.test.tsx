import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { Providers } from "@/app/providers";
import engineerReport from "@/fixtures/api/engineer_report_VER_ALO.json";
import { FOOTER_NOTE } from "@/lib/engineer";
import { useUiStore } from "@/lib/store";
import { signInAs, signOutForTest } from "@/test/auth";
import { apiError, server } from "@/test/msw";

import { EngineerChat, EngineerSection } from "./engineer";

const REF = { driver: "VER", lap: "fastest" as const };
const TGT = { driver: "ALO", lap: "fastest" as const };

function renderChat() {
  return render(
    <Providers demo={false}>
      <EngineerChat request={{ session_key: "2024-05-Q", reference: REF, target: TGT }} target="ALO" sessionCode="Q" />
    </Providers>,
  );
}

function renderEngineer() {
  return render(
    <Providers demo={false}>
      <EngineerSection session="2024-05-Q" reference={REF} target={TGT} sessionCode="Q" />
    </Providers>,
  );
}

beforeEach(() => {
  useUiStore.setState({ toasts: [], selectedCornerId: null, hoverDistance: null });
  signInAs("user");
});

describe("engineer report card", () => {
  it("renders the sections, the always-visible limitations block and the footer note", async () => {
    renderEngineer();
    const report = await screen.findByTestId("engineer-report");
    expect(within(report).getByText(/ALO was 0\.488 s slower than VER/)).toBeInTheDocument();
    expect(within(report).getByText("Biggest time loss")).toBeInTheDocument();
    expect(screen.getByTestId("engineer-limitations")).toHaveTextContent("Data limitations");
    expect(screen.getByTestId("engineer-footer")).toHaveTextContent(FOOTER_NOTE);
  });

  it("the fact popover shows the right fact on focus (keyboard) and hover", async () => {
    const user = userEvent.setup();
    renderEngineer();
    const report = await screen.findByTestId("engineer-report");
    const headline = within(report).getAllByTestId("engineer-bullet")[0]!;
    const indicator = within(headline).getByTestId("fact-indicator");
    await user.tab();
    indicator.focus();
    const pop = await within(headline).findByTestId("fact-popover");
    expect(pop).toHaveTextContent("LAP.time_loss_s");
    expect(pop).toHaveTextContent("0.488 s");
    expect(pop).toHaveTextContent("positive = target slower");
    expect(indicator).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(within(headline).queryByTestId("fact-popover")).toBeNull();
    await user.hover(indicator);
    expect(await within(headline).findByTestId("fact-popover")).toHaveTextContent("LAP.sector_3_delta_s");
  });

  it("shows the fallback badge with its reason for template reports", async () => {
    renderEngineer();
    const badge = await screen.findByTestId("engineer-badge");
    expect(badge).toHaveTextContent("Template summary");
    expect(badge).toHaveAttribute("title", expect.stringMatching(/No AI model is configured/));
  });

  it("shows the AI badge for LLM reports and the fact-check reason for validation fallbacks", async () => {
    server.use(http.post("*/api/v1/engineer/report", () =>
      HttpResponse.json({ ...engineerReport, generated_by: "llm", outcome: "llm_first_try", model: "m" })));
    const { unmount } = renderEngineer();
    expect(await screen.findByTestId("engineer-badge")).toHaveTextContent("AI-written from verified telemetry");
    unmount();
    server.use(http.post("*/api/v1/engineer/report", () =>
      HttpResponse.json({ ...engineerReport, outcome: "template_validation", fallback_reason: "ungrounded_number" })));
    renderEngineer();
    expect(await screen.findByTestId("engineer-fallback-note")).toHaveTextContent(/failed the fact check \(ungrounded_number\)/);
  });

  it("clicking a bullet highlights its segment (fact prefix → finding id)", async () => {
    const user = userEvent.setup();
    renderEngineer();
    const report = await screen.findByTestId("engineer-report");
    await user.click(within(report).getByRole("button", { name: /^Turn 16: ALO lost 0\.176 s/ }));
    expect(useUiStore.getState().selectedCornerId).toBe("2024-05-Q:ALO-vs-VER:T16");
  });

  it("regenerate has a 10 s cooldown", async () => {
    const user = userEvent.setup();
    renderEngineer();
    const button = await screen.findByTestId("engineer-regenerate");
    await user.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    expect(button.textContent).toMatch(/Regenerat/);
  });

  it("shows an error with the request id when the report fails", async () => {
    server.use(http.post("*/api/v1/engineer/report", () => apiError(500, "internal_error", "boom", "req-eng-1")));
    renderEngineer();
    const err = await screen.findByTestId("engineer-error");
    expect(err).toHaveTextContent("request_id: req-eng-1");
  });
});

describe("engineer chat", () => {
  it("asks via a suggestion chip and renders the answer in a live region with fact hovers", async () => {
    const user = userEvent.setup();
    renderChat();
    const chat = await screen.findByTestId("engineer-chat");
    const log = within(chat).getByRole("log");
    expect(log).toHaveAttribute("aria-live", "polite");
    await user.click(within(chat).getAllByTestId("engineer-chip")[0]!);
    const answer = await within(chat).findByTestId("engineer-answer");
    expect(answer).toHaveTextContent("Turn 16: ALO lost 0.176 s");
    expect(within(answer).getByTestId("fact-indicator")).toBeInTheDocument();
    // follow-up chips come from the answer
    expect(within(chat).getAllByTestId("engineer-chip").map((c) => c.textContent)).toContain(
      "Was the time loss caused by the driver, the tyres or traffic?");
  });

  it("limits the input to 300 characters", async () => {
    renderChat();
    const input = await screen.findByTestId("engineer-input");
    expect(input).toHaveAttribute("maxLength", "300");
  });

  it("styles out-of-scope answers neutrally", async () => {
    const user = userEvent.setup();
    renderChat();
    const input = await screen.findByTestId("engineer-input");
    await user.type(input, "Who will win the championship?{Enter}");
    const answer = await screen.findByTestId("engineer-answer");
    expect(answer).toHaveAttribute("data-variant", "neutral");
    expect(answer).toHaveTextContent("I can only discuss this comparison");
    expect(within(answer).queryByTestId("engineer-badge")).toBeNull();
  });

  it("shows a graceful inline error with the request id (no toast) and can retry", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/engineer/ask", () => apiError(503, "unavailable", "engineer offline", "req-ask-9")));
    renderChat();
    const input = await screen.findByTestId("engineer-input");
    await user.type(input, "Where did ALO lose time?{Enter}");
    const err = await screen.findByTestId("engineer-error");
    expect(err).toHaveTextContent("request_id: req-ask-9");
    expect(useUiStore.getState().toasts).toEqual([]);
    expect(within(err).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows a loading state while the engineer answers", async () => {
    const user = userEvent.setup();
    let release: () => void = () => {};
    server.use(http.post("*/api/v1/engineer/ask", async () => {
      await new Promise<void>((r) => { release = r; });
      return HttpResponse.json({});
    }));
    renderChat();
    await user.type(await screen.findByTestId("engineer-input"), "Where did ALO lose time?{Enter}");
    expect(await screen.findByTestId("engineer-thinking")).toHaveTextContent("Engineer is thinking");
    release();
  });
});

describe("engineer and accounts", () => {
  it("asks for a sign-in instead of calling the API when signed out", async () => {
    signOutForTest();
    renderEngineer();
    expect(await screen.findByTestId("signin-prompt")).toHaveTextContent("Sign in to get the AI race engineer");
    expect(screen.queryByTestId("engineer-report")).toBeNull();
  });
});
