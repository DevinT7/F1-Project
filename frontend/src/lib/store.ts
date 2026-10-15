/**
 * Small client store for ephemeral UI state that must be shared across components without
 * re-rendering the charts: hover distance (chart ↔ map), the selected corner, toasts.
 * Charts read hover/selection via useUiStore.subscribe and update Plotly shapes imperatively.
 */
import { create } from "zustand";

export interface Toast {
  id: number;
  title: string;
  message: string;
  code?: string;
  requestId?: string | null;
}

interface UiState {
  hoverDistance: number | null;
  selectedCornerId: string | null;
  toasts: Toast[];
  setHover: (distance: number | null) => void;
  selectCorner: (id: string | null) => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: number) => void;
}

let toastSeq = 0;
const MAX_TOASTS = 4;

export const useUiStore = create<UiState>((set) => ({
  hoverDistance: null,
  selectedCornerId: null,
  toasts: [],
  setHover: (distance) => set((s) => (s.hoverDistance === distance ? s : { hoverDistance: distance })),
  selectCorner: (id) => set({ selectedCornerId: id }),
  pushToast: (toast) =>
    set((s) => {
      // the same error twice in a row (e.g. a refetch) should not stack
      const last = s.toasts[s.toasts.length - 1];
      if (last && last.code === toast.code && last.message === toast.message) return s;
      return { toasts: [...s.toasts, { ...toast, id: ++toastSeq }].slice(-MAX_TOASTS) };
    }),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));
