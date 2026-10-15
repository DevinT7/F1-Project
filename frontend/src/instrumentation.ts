// Server-side Sentry for the Next.js server (Phase 9): rendering errors and failed server requests.
// Off unless SENTRY_DSN (or NEXT_PUBLIC_SENTRY_DSN) is set; no PII.
import * as Sentry from "@sentry/nextjs";

export function register() {
  const dsn = process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN;
  if (!dsn) return;
  Sentry.init({
    dsn,
    environment: process.env.APP_ENV ?? process.env.NEXT_PUBLIC_APP_ENV ?? "development",
    release: process.env.NEXT_PUBLIC_RELEASE || undefined,
    // no PII: no user fields, cookies, bodies or query strings (the request id is the correlation key)
    dataCollection: { userInfo: false, cookies: false, httpBodies: [], urlQueryParams: false,
                      httpHeaders: { request: { allow: ["x-request-id"] }, response: { allow: ["x-request-id"] } } },
    tracesSampleRate: Number(process.env.SENTRY_TRACES_SAMPLE_RATE ?? "0.05"),
  });
}

export const onRequestError = Sentry.captureRequestError;
