/**
 * Phase 8 client auth. The access token lives in memory only (never localStorage); the refresh
 * token is an httpOnly cookie the browser sends to /api/v1/auth/* (same origin via the Next proxy).
 * On load the session is restored with POST /auth/refresh; a 401 on an authed call triggers one
 * silent refresh and a retry (lib/api/client.ts).
 */
import { create } from "zustand";

import type { Schemas } from "./api/client";

export type AuthUser = Schemas["UserOut"];
export type TokenResponse = Schemas["TokenResponse"];
export const CSRF_HEADER = { "X-RaceIQ-CSRF": "1" } as const;

interface AuthState {
  status: "unknown" | "anonymous" | "signed_in";
  user: AuthUser | null;
  accessToken: string | null;
  setSession: (t: TokenResponse) => void;
  clear: () => void;
}

export const useAuth = create<AuthState>((set) => ({
  status: "unknown",
  user: null,
  accessToken: null,
  setSession: (t) => set({ status: "signed_in", user: t.user, accessToken: t.access_token }),
  clear: () => set({ status: "anonymous", user: null, accessToken: null }),
}));

export const isAdmin = (u: AuthUser | null | undefined): boolean => u?.role === "admin";

function base(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE ?? "";
  if (configured) return configured.replace(/\/$/, "");
  return typeof window !== "undefined" ? window.location.origin : "http://localhost:3000";
}

let refreshing: Promise<boolean> | null = null;

/** One refresh at a time; resolves true when a new access token was obtained. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const r = await fetch(`${base()}/api/v1/auth/refresh`, { method: "POST", headers: { ...CSRF_HEADER },
        credentials: "include" });
      if (!r.ok || r.status === 204) {                      // 204: no session (signed out)
        useAuth.getState().clear();
        return false;
      }
      useAuth.getState().setSession((await r.json()) as TokenResponse);
      return true;
    } catch {
      useAuth.getState().clear();
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function signOut(): Promise<void> {
  try {
    await fetch(`${base()}/api/v1/auth/logout`, { method: "POST", headers: { ...CSRF_HEADER }, credentials: "include" });
  } finally {
    useAuth.getState().clear();
  }
}
