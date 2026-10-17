"use client";

import { useSearchParams } from "next/navigation";

import { Dashboard } from "@/components/dashboard";

import { Providers } from "./providers";

export function DashboardPage() {
  const demoParam = useSearchParams().get("demo");
  const demo = demoParam === "1" || (process.env.NEXT_PUBLIC_DEMO_DEFAULT === "1" && demoParam !== "0");
  return (
    <Providers demo={demo}>
      <Dashboard />
    </Providers>
  );
}
