"use client";
/** Lets deep components switch tabs (e.g. an engineer bullet → its corner on the Track Map tab). */
import { createContext, useContext } from "react";

import type { TabId } from "./tabs";

export const TabNavContext = createContext<(tab: TabId) => void>(() => {});

export function useGoToTab(): (tab: TabId) => void {
  return useContext(TabNavContext);
}
