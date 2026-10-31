/** The Firebase Auth provider: ID token as bearer, `getIdToken(true)` for refreshed claims. */

import { type User, onAuthStateChanged, signOut } from "firebase/auth";

import { getFirebaseAuth } from "../firebase";
import type { AuthProvider, AuthUser } from "./types";

export const firebaseProvider: AuthProvider = {
  backend: "firebase",
  currentUser: () => getFirebaseAuth().currentUser,
  async getAuthHeaders() {
    const user = getFirebaseAuth().currentUser;
    if (!user) return null;
    const token = await user.getIdToken();
    return { Authorization: `Bearer ${token}` };
  },
  fetchCredentials: undefined,
  async refresh(user: AuthUser) {
    await (user as User).getIdToken(true);
  },
  signOut: () => signOut(getFirebaseAuth()),
  onChange: (cb) => onAuthStateChanged(getFirebaseAuth(), cb),
};
