/**
 * Typed fetch wrapper over the generated OpenAPI types (src/api/types.ts, `npm run gen-types`).
 * Request and response types are derived from `paths` — never hand-written.
 */
import type { components, paths } from "@/api/types";

import { refreshSession, useAuth } from "../auth";

export type Schemas = components["schemas"];
export type ApiErrorBody = Schemas["ErrorResponse"];

type JsonOf<R> = R extends { content: { "application/json": infer T } } ? T : never;
type Op<P extends keyof paths, M extends "get" | "post"> = NonNullable<paths[P][M]>;
type HasMethod<M extends "get" | "post"> = {
  [P in keyof paths]: paths[P][M] extends undefined | never ? never : P;
}[keyof paths];

export type GetPath = Extract<HasMethod<"get">, `/api/v1/${string}`>;
export type PostPath = Extract<HasMethod<"post">, `/api/v1/${string}`>;
type Params<O> = O extends { parameters: infer P } ? P : never;
type PathParams<O> = Params<O> extends { path: infer X } ? X : undefined;
type QueryParams<O> = Params<O> extends { query?: infer Q } ? Q : undefined;
// 200, or 202 for endpoints that start a background job (POST .../ingest)
type Ok<O> = O extends { responses: { 200: infer R } } ? JsonOf<R>
  : O extends { responses: { 202: infer R } } ? JsonOf<R> : never;
type Body<O> = O extends { requestBody: { content: { "application/json": infer B } } } ? B : undefined;

export type GetResponse<P extends GetPath> = Ok<Op<P, "get">>;
export type PostResponse<P extends PostPath> = Ok<Op<P, "post">>;

/** An API failure with the server's error envelope (code + request_id) when there was one. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, requestId: string | null = null, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

function apiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE ?? "";
  if (configured) return configured.replace(/\/$/, "");
  return typeof window !== "undefined" ? window.location.origin : "http://localhost:3000";
}

export function buildUrl(
  template: string,
  path?: Record<string, string | number>,
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  const filled = template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = path?.[name];
    if (value === undefined) throw new Error(`missing path parameter ${name} for ${template}`);
    return encodeURIComponent(String(value));
  });
  const url = new URL(filled, `${apiBase()}/`);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  return url.toString();
}

async function parse<T>(response: Response): Promise<T> {
  const requestId = response.headers.get("x-request-id");
  if (response.ok) return (await response.json()) as T;
  let body: Partial<ApiErrorBody> | null = null;
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    body = null;
  }
  const err = body?.error;
  throw new ApiError(
    response.status,
    err?.code ?? `http_${response.status}`,
    err?.message ?? (response.statusText || "Request failed"),
    err?.request_id ?? requestId,
    err?.details,
  );
}

async function rawSend(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (cause) {
    throw new ApiError(0, "network_error", "Cannot reach the RaceIQ API. Is the backend running on :8000?", null, cause);
  }
}

/** Adds the access token (when signed in); on a 401 tries one silent refresh and retries once. */
async function send(url: string, init: RequestInit): Promise<Response> {
  const withToken = (): RequestInit => {
    const token = useAuth.getState().accessToken;
    return token ? { ...init, headers: { ...(init.headers as Record<string, string>), Authorization: `Bearer ${token}` } } : init;
  };
  const response = await rawSend(url, withToken());
  if (response.status === 401 && useAuth.getState().accessToken && !url.includes("/api/v1/auth/")) {
    if (await refreshSession()) return rawSend(url, withToken());
  }
  return response;
}

export async function apiGet<P extends GetPath>(
  path: P,
  opts: { path?: PathParams<Op<P, "get">>; query?: QueryParams<Op<P, "get">>; signal?: AbortSignal } = {},
): Promise<GetResponse<P>> {
  const url = buildUrl(path, opts.path as Record<string, string | number> | undefined,
    opts.query as Record<string, string | number | boolean | null | undefined> | undefined);
  return parse<GetResponse<P>>(await send(url, { headers: { Accept: "application/json" }, signal: opts.signal }));
}

export async function apiPost<P extends PostPath>(
  path: P,
  opts: { path?: PathParams<Op<P, "post">>; query?: QueryParams<Op<P, "post">>; body?: Body<Op<P, "post">>; signal?: AbortSignal } = {},
): Promise<PostResponse<P>> {
  const url = buildUrl(path, opts.path as Record<string, string | number> | undefined,
    opts.query as Record<string, string | number | boolean | null | undefined> | undefined);
  return parse<PostResponse<P>>(await send(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
  }));
}
