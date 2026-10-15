import { Suspense } from "react";

import { DashboardPage } from "./dashboard-page";

// The whole page is driven by URL search params (?s=…&ref=…&tgt=…), so it renders on the client.
export default function Page() {
  return (
    <Suspense fallback={<div className="p-10 text-sm text-muted" role="status">Loading Senna…</div>}>
      <DashboardPage />
    </Suspense>
  );
}
