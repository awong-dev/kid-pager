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

export interface Me {
  uid: string;
  alias: string;
  displayName: string;
  role: Role;
}

export type AuthStatus = "loading" | "signed-out" | "not-registered" | "signed-in";

interface AuthContextValue {
  status: AuthStatus;
  firebaseUser: FirebaseUser | null;
  me: Me | null;
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

  const value: AuthContextValue = {
    status,
    firebaseUser,
    me,
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
