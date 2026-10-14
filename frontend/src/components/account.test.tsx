import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { Providers } from "@/app/providers";
import { apiGet } from "@/lib/api/client";
import { useAuth } from "@/lib/auth";
import { signInAs, signOutForTest } from "@/test/auth";
import { apiError, server } from "@/test/msw";

import { SignInDialog } from "./account";
import { EngineerSection } from "./engineer";

const TOKENS = (role: "user" | "admin" = "user", token = "fresh-token") => ({
  access_token: token, token_type: "bearer", expires_in: 900,
  user: { id: "00000000-0000-0000-0000-000000000002", email: "driver@example.com", role, created_at: "2026-10-14T00:00:00Z" },
});

function renderWithDialog() {
  return render(
    <Providers demo={false}>
      <EngineerSection session="2024-05-Q" reference={{ driver: "VER", lap: "fastest" }} target={{ driver: "ALO", lap: "fastest" }}
        sessionCode="Q" />
      <SignInDialog />
    </Providers>,
  );
}

beforeEach(() => {
  signOutForTest();
  // jsdom has no <dialog> modal API
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) { this.open = true; };
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) { this.open = false; };
});

describe("sign-in dialog", () => {
  it("signs in from the engineer prompt, then the report loads", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/login", () => HttpResponse.json(TOKENS())));
    renderWithDialog();
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    await user.type(screen.getByTestId("signin-email"), "driver@example.com");
    await user.type(screen.getByTestId("signin-password"), "correct horse battery");
    await user.click(screen.getByTestId("signin-submit"));
    expect(await screen.findByTestId("engineer-report")).toBeInTheDocument();
    expect(useAuth.getState().user?.email).toBe("driver@example.com");
    expect(localStorage.length).toBe(0);                            // the token is never persisted
  });

  it("shows a clear message for wrong credentials and for a taken email", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/auth/login", () => apiError(401, "invalid_credentials", "wrong email or password")),
      http.post("*/api/v1/auth/register", () => apiError(409, "email_taken", "taken")),
    );
    renderWithDialog();
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    await user.type(screen.getByTestId("signin-email"), "driver@example.com");
    await user.type(screen.getByTestId("signin-password"), "wrong password!!");
    await user.click(screen.getByTestId("signin-submit"));
    expect(await screen.findByTestId("signin-error")).toHaveTextContent("Wrong email or password.");
    await user.click(screen.getByRole("button", { name: "New here? Create an account" }));
    await user.click(screen.getByTestId("signin-submit"));
    expect(await screen.findByTestId("signin-error")).toHaveTextContent("already exists");
  });
});

describe("API client and tokens", () => {
  it("sends the access token and, on a 401, refreshes once and retries", async () => {
    signInAs("user");
    useAuth.setState({ accessToken: "stale-token" });
    const seen: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/me", ({ request }) => {
        const auth = request.headers.get("authorization");
        seen.push(auth);
        return auth === "Bearer fresh-token" ? HttpResponse.json(TOKENS().user) : apiError(401, "invalid_token", "token expired");
      }),
      http.post("*/api/v1/auth/refresh", ({ request }) =>
        request.headers.get("x-raceiq-csrf") === "1" ? HttpResponse.json(TOKENS()) : apiError(403, "csrf_failed", "csrf")),
    );
    const me = await apiGet("/api/v1/me");
    expect(me.email).toBe("driver@example.com");
    expect(seen).toEqual(["Bearer stale-token", "Bearer fresh-token"]);
  });

  it("a failed refresh signs the user out", async () => {
    signInAs("user");
    server.use(http.get("*/api/v1/me", () => apiError(401, "token_revoked", "ended")));
    await expect(apiGet("/api/v1/me")).rejects.toMatchObject({ status: 401 });
    await waitFor(() => expect(useAuth.getState().status).toBe("anonymous"));
  });
});
