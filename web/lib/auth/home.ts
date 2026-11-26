/** The home provider: relay sessions in an `HttpOnly` cookie; sends `X-Requested-With: pager`. */

import { withBase } from "../basePath";
import type { AuthProvider, AuthUser } from "./types";

const CSRF_HEADERS = { "X-Requested-With": "pager" };

export class HomeAuthError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { ...CSRF_HEADERS };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(withBase(`/api${path}`), {
    method,
    headers,
    credentials: "include",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!resp.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : undefined;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? "Check the fields and try again."
          : `request failed with status ${resp.status}`;
    throw new HomeAuthError(resp.status, message);
  }
  return data as T;
}

export interface HomeSession {
  uid: string;
  alias: string;
  displayName: string;
  hasPassword: boolean;
  passkeys: number;
}

let current: AuthUser | null = null;
const listeners = new Set<(user: AuthUser | null) => void>();

function publish(user: AuthUser | null) {
  current = user;
  for (const cb of listeners) cb(user);
}

/** Re-reads the session from the relay and tells every listener. */
export async function refreshSession(): Promise<HomeSession | null> {
  try {
    const session = await call<HomeSession>("GET", "/auth/session");
    publish({ uid: session.uid });
    return session;
  } catch {
    publish(null);
    return null;
  }
}

export const homeProvider: AuthProvider = {
  backend: "home",
  currentUser: () => current,
  getAuthHeaders: async () => ({ ...CSRF_HEADERS }),
  fetchCredentials: "include",
  refresh: async () => {},
  async signOut() {
    try {
      await call("POST", "/auth/logout");
    } finally {
      publish(null);
    }
  },
  onChange(cb) {
    listeners.add(cb);
    void refreshSession();
    return () => {
      listeners.delete(cb);
    };
  },
};

interface SignedIn {
  uid: string;
  alias: string;
}

async function signedIn(promise: Promise<SignedIn>): Promise<SignedIn> {
  const result = await promise;
  await refreshSession();
  return result;
}

export function loginWithPassword(username: string, password: string): Promise<SignedIn> {
  return signedIn(call<SignedIn>("POST", "/auth/login", { username, password }));
}

export function redeemInvite(code: string, password?: string): Promise<SignedIn> {
  return signedIn(
    call<SignedIn>("POST", "/auth/invite/redeem", password ? { code, password } : { code })
  );
}

export function getSetupStatus(): Promise<{ needsSetup: boolean }> {
  return call<{ needsSetup: boolean }>("GET", "/setup/status");
}

export function createFirstAdmin(username: string, password: string): Promise<SignedIn> {
  return signedIn(call<SignedIn>("POST", "/setup/first-admin", { username, password }));
}

// Passkeys: the relay uses JSON with base64url binary fields; the browser wants ArrayBuffers.

function fromB64url(value: string): ArrayBuffer {
  const b64 = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function toB64url(buf: ArrayBuffer | null | undefined): string | null {
  if (!buf) return null;
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type Descriptor = { id: string; type: "public-key"; transports?: AuthenticatorTransport[] };

function descriptors(list: Descriptor[] | undefined): PublicKeyCredentialDescriptor[] | undefined {
  return list?.map((d) => ({ ...d, id: fromB64url(d.id) }));
}

/** True when this browser can use passkeys here (secure context with WebAuthn). */
export function passkeysAvailable(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && "PublicKeyCredential" in window;
}

interface Begin {
  challengeId: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  options: any;
}

/** Adds a passkey to the signed-in account. */
export async function registerPasskey(): Promise<void> {
  const begin = await call<Begin>("POST", "/auth/passkey/register/begin");
  const o = begin.options;
  const publicKey: PublicKeyCredentialCreationOptions = {
    ...o,
    challenge: fromB64url(o.challenge),
    user: { ...o.user, id: fromB64url(o.user.id) },
    excludeCredentials: descriptors(o.excludeCredentials),
  };
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new HomeAuthError(400, "No passkey was created.");
  const response = cred.response as AuthenticatorAttestationResponse;
  const credential = {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      attestationObject: toB64url(response.attestationObject),
      transports: response.getTransports?.() ?? [],
    },
    clientExtensionResults: cred.getClientExtensionResults(),
    authenticatorAttachment: cred.authenticatorAttachment ?? undefined,
  };
  await call("POST", "/auth/passkey/register/finish", { challengeId: begin.challengeId, credential });
  await refreshSession();
}

/** Sign in with a discoverable passkey; no username. */
export async function loginWithPasskey(): Promise<SignedIn> {
  const begin = await call<Begin>("POST", "/auth/passkey/login/begin");
  const o = begin.options;
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...o,
    challenge: fromB64url(o.challenge),
    allowCredentials: descriptors(o.allowCredentials),
  };
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new HomeAuthError(400, "No passkey was chosen.");
  const response = cred.response as AuthenticatorAssertionResponse;
  const credential = {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      authenticatorData: toB64url(response.authenticatorData),
      signature: toB64url(response.signature),
      userHandle: toB64url(response.userHandle),
    },
    clientExtensionResults: cred.getClientExtensionResults(),
    authenticatorAttachment: cred.authenticatorAttachment ?? undefined,
  };
  return signedIn(
    call<SignedIn>("POST", "/auth/passkey/login/finish", { challengeId: begin.challengeId, credential })
  );
}
