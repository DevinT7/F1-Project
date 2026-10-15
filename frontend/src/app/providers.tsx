"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import { createApi } from "@/lib/api/endpoints";
import { ApiContext } from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import { reportError } from "@/lib/telemetry";

/** Every failed API call surfaces as a toast with its error code and request id, except codes a
 * query declares it handles itself (meta.handledCodes: e.g. the tyre panel's 503 state). Network
 * failures and 5xx also go to Sentry (when configured), tagged with the request id. */
function toastFor(error: unknown, meta?: Record<string, unknown>) {
  const err = error instanceof ApiError ? error : null;
  const handled = meta?.handledCodes;
  if (err && Array.isArray(handled) && handled.includes(err.code)) return;
  reportError(error);
  useUiStore.getState().pushToast({
    title: err?.status === 0 ? "Backend unreachable" : "Request failed",
    message: err?.message ?? (error instanceof Error ? error.message : String(error)),
    code: err?.code ?? "client_error",
    requestId: err?.requestId ?? null,
  });
}

export function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({ onError: (error, query) => toastFor(error, query.meta) }),
    // a mutation that renders its own error (meta.inlineErrors, e.g. the engineer chat) is not toasted
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => { if (!mutation.meta?.inlineErrors) toastFor(error); },
    }),
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        // never retry client errors (bad lap, not ingested...); retry flaky network once
        retry: (count, error) => error instanceof ApiError && (error.status === 0 || error.status >= 500) && count < 1,
      },
    },
  });
}

export function Providers({ demo, children }: { demo: boolean; children: ReactNode }) {
  const [client] = useState(makeQueryClient);
  const api = useMemo(() => ({ api: createApi(demo), demo }), [demo]);
  return (
    <QueryClientProvider client={client}>
      <ApiContext.Provider value={api}>{children}</ApiContext.Provider>
    </QueryClientProvider>
  );
}
