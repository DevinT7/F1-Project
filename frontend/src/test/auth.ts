/** Test helper: put the client into a signed-in state (the access token lives in memory only). */
import { useAuth } from "@/lib/auth";

export function signInAs(role: "user" | "admin" = "user") {
  useAuth.setState({
    status: "signed_in", accessToken: "test-token",
    user: { id: "00000000-0000-0000-0000-000000000001", email: `${role}@example.com`, role, created_at: "2026-10-14T00:00:00Z" },
  });
}

export function signOutForTest() {
  useAuth.setState({ status: "anonymous", accessToken: null, user: null });
}
