/** Sign-in provider interface: Firebase Auth or relay sessions (home). */

/** The signed-in account as far as the provider knows it. Under firebase
 * this is the Firebase `User` itself (which has `uid`). */
export interface AuthUser {
  uid: string;
}

export type AuthBackend = "firebase" | "home";

export interface AuthProvider {
  backend: AuthBackend;
  /** The current account, or `null` while signed out. */
  currentUser(): AuthUser | null;
  /** Headers for a relay call, or `null` when there is no signed-in
   * account to send (the caller answers 401 itself). Firebase: the ID token
   * as a bearer. Home: the CSRF header; the cookie carries the session. */
  getAuthHeaders(): Promise<Record<string, string> | null>;
  /** `fetch`'s `credentials` for relay calls: `undefined` keeps the
   * browser default (firebase), `"include"` sends the session cookie. */
  fetchCredentials: RequestCredentials | undefined;
  /** Forces a fresh credential (firebase: a new ID token with updated claims); no-op at home. */
  refresh(user: AuthUser): Promise<void>;
  signOut(): Promise<void>;
  /** Calls `cb` with the current account and on every change; returns the unsubscribe. */
  onChange(cb: (user: AuthUser | null) => void): () => void;
}
