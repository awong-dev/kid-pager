/** Picks the sign-in provider at build time via `NEXT_PUBLIC_BACKEND` (`firebase` default or `home`). */

import { firebaseProvider } from "./firebase";
import { homeProvider } from "./home";
import type { AuthBackend, AuthProvider } from "./types";

export type { AuthBackend, AuthProvider, AuthUser } from "./types";

export const AUTH_BACKEND: AuthBackend =
  process.env.NEXT_PUBLIC_BACKEND === "home" ? "home" : "firebase";

export const authProvider: AuthProvider = AUTH_BACKEND === "home" ? homeProvider : firebaseProvider;
