/**
 * `fetch` wrapper for every relay write: the base path is the relative `/api`, with auth
 * headers (and credentials) from the auth provider.
 */

import { authProvider } from "./auth";

// A policy-gated 403 body is `{reason, message}` (nested under `detail` or bare); `reason` is
// a machine code and `message` is shown verbatim.
function parseReasonMessage(detail: unknown): { reason?: string; message?: string } | null {
  if (!detail || typeof detail !== "object") return null;
  const d = detail as Record<string, unknown>;
  if (typeof d.reason !== "string" && typeof d.message !== "string") return null;
  return {
    reason: typeof d.reason === "string" ? d.reason : undefined,
    message: typeof d.message === "string" ? d.message : undefined,
  };
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  /** Reason code from a `{reason, message}` 403 body, else `undefined`. */
  reason?: string;

  constructor(status: number, detail: unknown) {
    const parsed = parseReasonMessage(detail);
    super(parsed?.message ?? (typeof detail === "string" ? detail : `request failed with status ${status}`));
    this.status = status;
    this.detail = detail;
    this.reason = parsed?.reason;
  }
}

async function authHeader(): Promise<Record<string, string>> {
  const headers = await authProvider.getAuthHeaders();
  if (!headers) {
    throw new ApiError(401, "not signed in");
  }
  return headers;
}

async function request<T>(
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown
): Promise<T> {
  const headers: Record<string, string> = { ...(await authHeader()) };
  let payload: string | undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const init: RequestInit = { method, headers, body: payload };
  if (authProvider.fetchCredentials) init.credentials = authProvider.fetchCredentials;
  const resp = await fetch(`/api${path}`, init);
  const text = await resp.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!resp.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(resp.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
};
