"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { parseUrlState, serializeUrlState, type UrlState } from "./url-state";

/** URL search params ⇄ UrlState. Updates use router.replace so the URL is always shareable. */
export function useUrlState(): [UrlState, (patch: Partial<UrlState>) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const state = useMemo(() => parseUrlState(params.toString()), [params]);

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      const next = serializeUrlState({ ...parseUrlState(window.location.search), ...patch });
      router.replace(next ? `${pathname}?${next}` : pathname, { scroll: false });
    },
    [router, pathname],
  );
  return [state, update];
}
