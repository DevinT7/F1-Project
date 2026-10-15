/**
 * Phase 9 telemetry: request ids and Sentry.
 *
 * Every API call carries an `X-Request-ID` the browser generated; the backend binds it into its JSON logs
 * and echoes it (and puts it in error envelopes), so a toast's "request id" finds the server's log lines.
 * Errors worth a human (network failures, 5xx, render crashes) go to Sentry tagged with that id, only when
 * NEXT_PUBLIC_SENTRY_DSN is set at build time (src/instrumentation-client.ts). No PII: no user, no bodies.
 */
import * as Sentry from "@sentry/nextjs";

import { ApiError } from "./api/client";

/** 32 hex chars: what the backend accepts as a client-supplied id (^[A-Za-z0-9._-]{1,64}$). */
export function newRequestId(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID().replace(/-/g, "");
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

/** Client errors (4xx: bad input, not ingested, signed out) are the UI's business, not an incident. */
export function isReportable(error: unknown): boolean {
  if (error instanceof ApiError) return error.status === 0 || error.status >= 500;
  return true;
}

export function reportError(error: unknown, context: Record<string, string | undefined> = {}): void {
  if (!process.env.NEXT_PUBLIC_SENTRY_DSN || !isReportable(error)) return;
  const tags: Record<string, string> = {};
  if (error instanceof ApiError) {
    tags.api_code = error.code;
    tags.api_status = String(error.status);
    if (error.requestId) tags.request_id = error.requestId;
  }
  for (const [k, v] of Object.entries(context)) if (v) tags[k] = v;
  Sentry.captureException(error, { tags });
}
