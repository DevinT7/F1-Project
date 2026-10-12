"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import { createApi } from "@/lib/api/endpoints";
import { ApiContext } from "@/lib/queries";
import { useUiStore } from "@/lib/store";

/** Every failed API call surfaces as a toast with its error code and request id. */
function toastFor(error: unknown) {
  const err = error instanceof ApiError ? error : null;
  useUiStore.getState().pushToast({
    title: err?.status === 0 ? "Backend unreachable" : "Request failed",
    message: err?.message ?? (error instanceof Error ? error.message : String(error)),
    code: err?.code ?? "client_error",
    requestId: err?.requestId ?? null,
  });
}

export function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({ onError: toastFor }),
    mutationCache: new MutationCache({ onError: toastFor }),
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
