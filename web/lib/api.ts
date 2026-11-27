/**
 * `fetch` wrapper for every relay write: the base path is the relative `/api`, with auth
 * headers (and credentials) from the auth provider.
 */

import { authProvider } from "./auth";
import { withBase } from "./basePath";

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

/** A server frozen for a move refuses every change (`503 {"detail": "moving"}`). */
const MOVING_MESSAGE = "This server has moved and is read-only. Use the new server to make changes.";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  /** The whole parsed error body, for routes that put extras beside `detail`; `undefined` when empty. */
  body?: unknown;
  /** Reason code from a `{reason, message}` 403 body, else `undefined`. */
  reason?: string;

  constructor(status: number, detail: unknown, body?: unknown) {
    const parsed = parseReasonMessage(detail);
    super(parsed?.message ?? (typeof detail === "string" ? detail : `request failed with status ${status}`));
    this.status = status;
    this.detail = detail;
    this.body = body;
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
  const resp = await fetch(withBase(`/api${path}`), init);
  const text = await resp.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!resp.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(resp.status, resp.status === 503 && detail === "moving" ? MOVING_MESSAGE : detail, data);
  }
  return data as T;
}

/** A non-JSON request (file upload / download) with the same auth and error
 * mapping as `request()`; returns the raw `Response` once it is `ok`. */
async function requestRaw(
  method: "GET" | "POST",
  path: string,
  extraHeaders: Record<string, string>,
  body?: Blob
): Promise<Response> {
  const headers: Record<string, string> = { ...(await authHeader()), ...extraHeaders };
  const init: RequestInit = { method, headers, body };
  if (authProvider.fetchCredentials) init.credentials = authProvider.fetchCredentials;
  const resp = await fetch(withBase(`/api${path}`), init);
  if (!resp.ok) {
    const text = await resp.text();
    let data: unknown;
    try {
      data = text ? JSON.parse(text) : undefined;
    } catch {
      data = text;
    }
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new ApiError(resp.status, detail, data);
  }
  return resp;
}

export const api = {
  /** GET a binary body (e.g. a `.kpfam` export) as a Blob. */
  getBlob: async (path: string, headers: Record<string, string> = {}) =>
    (await requestRaw("GET", path, headers)).blob(),
  /** POST a raw file body (`application/octet-stream`) and parse the JSON answer. */
  postFile: async <T>(path: string, file: Blob, headers: Record<string, string> = {}) =>
    (await requestRaw("POST", path, { "Content-Type": "application/octet-stream", ...headers }, file)).json() as Promise<T>,
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
};

/** Give an existing person without an account a sign-in email (cloud build).
 * POST /api/family/people/{uid}/account {email}. */
export function createPersonAccount(uid: string, email: string, query = ""): Promise<unknown> {
  return api.post(`/family/people/${encodeURIComponent(uid)}/account${query}`, { email });
}
