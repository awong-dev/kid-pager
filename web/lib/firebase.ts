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

// `||`, not `??`: an unset workflow variable is inlined as `""`, which `??` would keep.
const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY || "demo-api-key",
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "demo-pager.firebaseapp.com",
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID || "demo-pager",
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET || "demo-pager.appspot.com",
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID || "0",
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID || "1:0:web:0",
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
