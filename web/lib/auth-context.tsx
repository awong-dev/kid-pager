"use client";

/** Auth state: calls `GET /api/me` on every sign-in; a 403 means signed in but not registered. */

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

import { ApiError, api } from "./api";
import type { PolicyDoc, Role, UserKind } from "./types";

export interface Me {
  uid: string;
  alias: string;
  displayName: string;
  role: Role;
  familyId: string | null;
  kind: UserKind;
  policy: PolicyDoc;
  notify: { alerts: boolean };
}

// The raw `/api/me` shape; optional fields are defaulted by `normalizeMe`.
type MeResponse = Partial<Omit<Me, "uid" | "alias" | "displayName" | "role">> &
  Pick<Me, "uid" | "alias" | "displayName" | "role">;

function normalizeMe(user: MeResponse): Me {
  return {
    uid: user.uid,
    alias: user.alias,
    displayName: user.displayName,
    role: user.role,
    familyId: user.familyId ?? null,
    kind: user.kind ?? "person",
    policy: user.policy ?? { out: "people", in: "people" },
    notify: user.notify ?? { alerts: true },
  };
}

export type AuthStatus = "loading" | "signed-out" | "not-registered" | "signed-in";

interface AuthContextValue {
  status: AuthStatus;
  firebaseUser: FirebaseUser | null;
  me: Me | null;
  /** `role === 'super'` -- the one global admin. */
  isSuper: boolean;
  /** `role === 'admin' || isSuper`. */
  isFamilyAdmin: boolean;
  /** @deprecated alias for `isFamilyAdmin`. */
  isAdmin: boolean;
  notRegisteredMessage: string | null;
  signOutUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const NOT_REGISTERED_MESSAGE =
  "This account is not registered with the pager. Ask your admin to add you.";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [firebaseUser, setFirebaseUser] = useState<FirebaseUser | null>(null);
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
      if (!user) {
        setMe(null);
        setStatus("signed-out");
        return;
      }
      setStatus("loading");
      try {
        let resp = await api.get<{ user: MeResponse; role: Role; claimsStale?: boolean }>(
          "/me"
        );
        if (resp.claimsStale) {
          // The server rewrote this account's claims: refresh the token, refetch once.
          resp = await api.get<{ user: MeResponse; role: Role; claimsStale?: boolean }>("/me");
        }
        setMe(normalizeMe(resp.user));
        setStatus("signed-in");
      } catch (err) {
        setMe(null);
        if (err instanceof ApiError && err.status === 403) {
          setStatus("not-registered");
        } else {
          // A transient failure counts as signed out so the UI does not stay "loading".
          setStatus("signed-out");
        }
      }
    });
    return unsubscribe;
  }, []);

  const signOutUser = useCallback(async () => {
    setMe(null);
    setStatus("signed-out");
  }, []);

  const isSuper = me?.role === "super";
  const isFamilyAdmin = me?.role === "admin" || isSuper;

  const value: AuthContextValue = {
    status,
    firebaseUser,
    me,
    isSuper,
    isFamilyAdmin,
    isAdmin: isFamilyAdmin,
    notRegisteredMessage: status === "not-registered" ? NOT_REGISTERED_MESSAGE : null,
    signOutUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}
