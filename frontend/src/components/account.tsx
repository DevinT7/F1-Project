"use client";
/**
 * Phase 8 accounts in the UI: the topbar account menu (sign in / register, saved comparisons,
 * sign out), the sign-in dialog, and the prompt shown where an account is required.
 */
import Link from "next/link";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { ApiError } from "@/lib/api/client";
import type { AnalysisParams } from "@/lib/api/endpoints";
import { isAdmin, signOut, useAuth } from "@/lib/auth";
import { useAnalyses, useApi, useSaveAnalysis } from "@/lib/queries";
import { serializeUrlState, type UrlState } from "@/lib/url-state";

// ---------------------------------------------------------------- sign-in dialog (shared)
interface DialogState { open: boolean; reason: string | null; show: (reason?: string) => void; hide: () => void }
let dialogListeners: ((s: { open: boolean; reason: string | null }) => void)[] = [];
let dialogState = { open: false, reason: null as string | null };
const setDialog = (next: { open: boolean; reason: string | null }) => {
  dialogState = next;
  dialogListeners.forEach((l) => l(next));
};

export function useSignInDialog(): DialogState {
  const [s, setS] = useState(dialogState);
  useEffect(() => {
    dialogListeners.push(setS);
    return () => { dialogListeners = dialogListeners.filter((l) => l !== setS); };
  }, []);
  return { ...s, show: (reason) => setDialog({ open: true, reason: reason ?? null }), hide: () => setDialog({ open: false, reason: null }) };
}

export function SignInDialog() {
  const { api } = useApi();
  const setSession = useAuth((s) => s.setSession);
  const dialog = useSignInDialog();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (dialog.open && !el.open) el.showModal?.();
    if (!dialog.open && el.open) el.close?.();
  }, [dialog.open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const t = mode === "login" ? await api.login(email, password) : await api.register(email, password);
      setSession(t);
      setPassword("");
      dialog.hide();
    } catch (err) {
      const api = err instanceof ApiError ? err : null;
      setError(api?.code === "invalid_credentials" ? "Wrong email or password."
        : api?.code === "email_taken" ? "An account with this email already exists; sign in instead."
          : api?.code === "rate_limited" ? api.message
            : api?.status === 422 ? "Use a valid email and a password of at least 10 characters."
              : (err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog ref={ref} aria-labelledby={titleId} onClose={() => dialog.hide()} data-testid="signin-dialog"
      className="m-auto w-[min(380px,92vw)] rounded-2xl border border-line bg-card p-5 text-ink backdrop:bg-black/50">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <h2 id={titleId} className="text-lg font-bold">{mode === "login" ? "Sign in" : "Create an account"}</h2>
        {dialog.reason && <p className="text-sm text-muted">{dialog.reason}</p>}
        <label className="flex flex-col gap-1 text-sm">Email
          <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)}
            className="rounded-lg border border-line bg-card-2 px-3 py-2" data-testid="signin-email" />
        </label>
        <label className="flex flex-col gap-1 text-sm">Password
          <input type="password" required minLength={10} autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password} onChange={(e) => setPassword(e.target.value)}
            className="rounded-lg border border-line bg-card-2 px-3 py-2" data-testid="signin-password" />
        </label>
        {error && <p className="text-sm text-red-ink" role="alert" data-testid="signin-error">{error}</p>}
        <div className="flex items-center justify-between gap-2">
          <button type="button" className="text-sm text-muted underline" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}>
            {mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}
          </button>
          <div className="flex gap-2">
            <button type="button" className="btn btn-ghost" onClick={() => dialog.hide()}>Cancel</button>
            <button type="submit" className="btn" disabled={busy} data-testid="signin-submit">
              {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
            </button>
          </div>
        </div>
      </form>
    </dialog>
  );
}

// ---------------------------------------------------------------- topbar menu
export function AccountMenu({ demo, current }: { demo: boolean; current: UrlState }) {
  const status = useAuth((s) => s.status);
  const user = useAuth((s) => s.user);
  const dialog = useSignInDialog();
  const [open, setOpen] = useState(false);
  const analyses = useAnalyses(status === "signed_in" && open);
  const save = useSaveAnalysis();
  if (demo) return null;
  if (status !== "signed_in" || !user) {
    return (
      <button type="button" className="btn btn-ghost" onClick={() => dialog.show()} data-testid="signin-open"
        disabled={status === "unknown"}>
        ⎆ Sign in
      </button>
    );
  }
  const canSave = Boolean(current.session && current.ref && current.tgt);
  function saveCurrent() {
    if (!current.session || !current.ref || !current.tgt) return;
    const params: AnalysisParams = { session_key: current.session, reference: { ...current.ref }, target: { ...current.tgt },
      resolution_m: current.resolution };
    save.mutate({ title: `${current.session} · ${current.ref.driver} vs ${current.tgt.driver}`, params });
  }
  return (
    <div className="relative">
      <button type="button" className="btn btn-ghost" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen((o) => !o)}
        data-testid="account-menu">
        ◉ {user.email}{isAdmin(user) && <span className="chip ml-1.5">admin</span>}
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-40 mt-2 w-72 rounded-xl border border-line bg-card p-3 text-sm shadow-lg">
          <div className="kick mb-1.5">Saved comparisons</div>
          <button type="button" role="menuitem" className="btn btn-ghost mb-2 w-full text-xs" disabled={!canSave || save.isPending}
            onClick={saveCurrent} data-testid="save-analysis">
            {save.isSuccess ? "Saved ✓" : "＋ Save this comparison"}
          </button>
          <ul className="max-h-56 overflow-y-auto" data-testid="saved-analyses">
            {analyses.data?.analyses.length === 0 && <li className="text-muted">Nothing saved yet.</li>}
            {analyses.data?.analyses.map((a) => {
              const p = a.params as unknown as AnalysisParams;
              const href = `?${serializeUrlState({ session: p.session_key, ref: { driver: p.reference.driver, lap: p.reference.lap ?? "fastest" },
                tgt: { driver: p.target.driver, lap: p.target.lap ?? "fastest" }, resolution: p.resolution_m ?? 2, demo: false })}`;
              return <li key={a.id}><Link role="menuitem" className="block rounded px-2 py-1 hover:bg-card-2" href={href}>{a.title}</Link></li>;
            })}
          </ul>
          <button type="button" role="menuitem" className="btn btn-ghost mt-2 w-full text-xs" onClick={() => { setOpen(false); void signOut(); }}
            data-testid="signout">Sign out</button>
        </div>
      )}
    </div>
  );
}

/** Where an account is needed (the AI engineer). */
export function SignInPrompt({ what, testId = "signin-prompt" }: { what: string; testId?: string }) {
  const dialog = useSignInDialog();
  return (
    <div className="rounded-lg border border-dashed border-line bg-card-2 p-4 text-sm" data-testid={testId}>
      <p className="text-ink-2">{what}</p>
      <button type="button" className="btn mt-3" onClick={() => dialog.show(what)}>Sign in</button>
    </div>
  );
}
