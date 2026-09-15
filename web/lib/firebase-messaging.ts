import { type Messaging, getMessaging, isSupported } from "firebase/messaging";

import { getFirebaseApp } from "./firebase";

// Messaging is unavailable at prerender and in some browsers; the support probe is memoized.
let messagingPromise: Promise<Messaging | null> | null = null;

export function getMessagingIfSupported(): Promise<Messaging | null> {
  if (!messagingPromise) {
    messagingPromise = isSupported()
      .then((ok) => (ok ? getMessaging(getFirebaseApp()) : null))
      .catch(() => null);
  }
  return messagingPromise;
}
