"use client";

import { useEffect } from "react";

import { reportError } from "@/lib/telemetry";

export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => reportError(error, { digest: error.digest }), [error]);
  return (
    <main className="mx-auto max-w-xl p-10">
      <div className="card" role="alert">
        <div className="kick">Something went wrong</div>
        <h1 className="mt-1 text-lg font-bold">The dashboard hit an unexpected error.</h1>
        <p className="mt-2 text-sm text-muted">{error.message}</p>
        {error.digest && <p className="mt-1 text-xs text-muted">Reference: {error.digest}</p>}
        <button className="btn mt-4" onClick={reset}>Try again</button>
      </div>
    </main>
  );
}
