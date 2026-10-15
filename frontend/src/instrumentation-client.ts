// Browser-side Sentry (Phase 9). Off unless NEXT_PUBLIC_SENTRY_DSN was set at build time; no PII, no
// session replay. Errors are reported through src/lib/telemetry.ts (5xx / network / render crashes).
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_APP_ENV ?? "development",
    release: process.env.NEXT_PUBLIC_RELEASE || undefined,
    // no PII: no user fields, cookies, bodies or query strings (the request id is the correlation key)
    dataCollection: { userInfo: false, cookies: false, httpBodies: [], urlQueryParams: false,
                      httpHeaders: { request: { allow: ["x-request-id"] }, response: { allow: ["x-request-id"] } } },
    tracesSampleRate: Number(process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE ?? "0.05"),
  });
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
