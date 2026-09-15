/** Firebase JS SDK init; `NEXT_PUBLIC_USE_EMULATORS` connects the Auth and Firestore emulators. */

import { type FirebaseApp, getApps, initializeApp } from "firebase/app";
import {
  type Auth,
  connectAuthEmulator,
  getAuth,
} from "firebase/auth";
import {
  type Firestore,
  connectFirestoreEmulator,
  getFirestore,
} from "firebase/firestore";

const firebaseConfig = {
};

const USE_EMULATORS = process.env.NEXT_PUBLIC_USE_EMULATORS === "1";
const FIRESTORE_EMULATOR_HOST = process.env.NEXT_PUBLIC_FIRESTORE_EMULATOR_HOST ?? "localhost";
const FIRESTORE_EMULATOR_PORT = Number(
  process.env.NEXT_PUBLIC_FIRESTORE_EMULATOR_PORT ?? "8080"
);
const AUTH_EMULATOR_URL =
  process.env.NEXT_PUBLIC_AUTH_EMULATOR_URL ?? "http://localhost:9099";

let app: FirebaseApp | undefined;
let auth: Auth | undefined;
let db: Firestore | undefined;

export function getFirebaseApp(): FirebaseApp {
  if (!app) {
    app = getApps().length ? getApps()[0]! : initializeApp(firebaseConfig);
  }
  return app;
}

export function getFirebaseAuth(): Auth {
  if (!auth) {
    auth = getAuth(getFirebaseApp());
    if (USE_EMULATORS) {
      // The connect calls throw if repeated on one instance (dev fast-refresh); guarded above.
      connectAuthEmulator(auth, AUTH_EMULATOR_URL, { disableWarnings: true });
    }
  }
  return auth;
}

export function getFirestoreDb(): Firestore {
  if (!db) {
    db = getFirestore(getFirebaseApp());
    if (USE_EMULATORS) {
      connectFirestoreEmulator(db, FIRESTORE_EMULATOR_HOST, FIRESTORE_EMULATOR_PORT);
    }
  }
  return db;
}

export { USE_EMULATORS };
