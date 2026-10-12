"use client";

import { useSearchParams } from "next/navigation";

import { Dashboard } from "@/components/dashboard";

import { Providers } from "./providers";

export function DashboardPage() {
  const demo = useSearchParams().get("demo") === "1";
  return (
    <Providers demo={demo}>
      <Dashboard />
    </Providers>
  );
}
