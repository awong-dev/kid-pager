/** Browser Notifications and push registration; permission is requested only from
 * `/account/notifications`, never on load. */

import { getToken } from "firebase/messaging";
import { ApiError, api } from "./api";
import { AUTH_BACKEND } from "./auth";
import { withBase } from "./basePath";
import { getMessagingIfSupported } from "./firebase-messaging";
import { type PushPath, ensurePushSubscription, sameKey, urlBase64ToBytes } from "./webpush";

export type { PushPath } from "./webpush";

/** The app's one service worker (`web/sw/sw.template.js`). */
const SW_PATH = "/sw.js"; // logical; `swUrl()` adds the runtime prefix
const swUrl = (): string => withBase(SW_PATH);

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notificationPermission(): NotificationPermission | "unsupported" {
  if (!notificationsSupported()) return "unsupported";
  return Notification.permission;
}

/** The relay's VAPID public key, or `null` when none is configured (503 or 404). */
export async function getVapidKey(): Promise<string | null> {
  try {
    const { publicKey } = await api.get<{ publicKey: string }>("/push/vapid-public-key");
    return publicKey || null;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 503)) return null;
    throw err;
  }
}

async function fcmToken(registration: ServiceWorkerRegistration): Promise<string | null> {
  const messaging = await getMessagingIfSupported();
  if (!messaging) return null;
  const token = await getToken(messaging, {
    vapidKey: process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY,
    serviceWorkerRegistration: registration,
  });
  return token || null;
}

/** Asks permission, registers `/sw.js` and this browser with the relay (Web Push if the relay has
 * a VAPID key, else an FCM token in the firebase build). Returns the active path or `null`. */
export async function registerForPush(): Promise<PushPath | null> {
  if (!notificationsSupported() || !("serviceWorker" in navigator)) return null;
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return null;

  await navigator.serviceWorker.register(swUrl(), { scope: withBase("/") });
  const registration = await navigator.serviceWorker.ready;
  if (!("pushManager" in registration)) return null;
  return ensurePushSubscription({
    backend: AUTH_BACKEND,
    pushManager: registration.pushManager,
    getVapidKey,
    putSubscription: (body) => api.put("/me/push-subscriptions", body),
    getFcmToken: () => fcmToken(registration),
    postFcmToken: (token) => api.post("/me/push-tokens", { token }),
    // Only the relay's row: the SDK's `deleteToken()` would also
    // unsubscribe the registration's (by now the relay's) subscription.
    deleteFcmToken: (token) => api.del(`/me/push-tokens/${encodeURIComponent(token)}`),
    userAgent: navigator.userAgent,
  });
}

/** Which path this browser would use, without prompting; `subscribed` if it already holds one. */
export async function currentPushPath(): Promise<{ path: PushPath | null; subscribed: boolean }> {
  const vapidKey = await getVapidKey();
  const path: PushPath | null = vapidKey ? "webpush" : AUTH_BACKEND === "firebase" ? "fcm" : null;
  if (!notificationsSupported() || !("serviceWorker" in navigator) || !path) {
    return { path, subscribed: false };
  }
  const registration = await navigator.serviceWorker.getRegistration(swUrl());
  const sub = registration ? await registration.pushManager.getSubscription() : null;
  if (!sub) return { path, subscribed: false };
  const isRelayKey = vapidKey !== null && sameKey(sub.options.applicationServerKey, urlBase64ToBytes(vapidKey));
  return { path, subscribed: path === "webpush" ? isRelayKey : !isRelayKey };
}

/** Chrome for Android throws "Illegal constructor" on `new Notification()`, so this uses the
 * service worker's `showNotification` (its click handler reads `data.url`) when registered. */
async function showNotification(
  title: string,
  options: NotificationOptions,
  url: string
): Promise<void> {
  let registration: ServiceWorkerRegistration | undefined;
  if ("serviceWorker" in navigator) {
    registration = await navigator.serviceWorker.getRegistration(swUrl());
  }
  if (registration) {
    await registration.showNotification(title, { ...options, data: { url } });
    return;
  }
  const n = new Notification(title, options);
  n.onclick = () => {
    window.focus();
    // Full navigation, not `useRouter()` -- this runs outside React.
    window.location.href = withBase(url);
  };
}

/** Local-only check that permission and display work (the relay has no on-demand push). */
export async function showLocalTestNotification(): Promise<void> {
  if (notificationPermission() !== "granted") {
    throw new Error("Notification permission is not granted.");
  }
  await showNotification(
    "Pager test notification",
    {
      body: "If you can see this, browser notifications are working.",
      icon: withBase("/icons/icon-192.png"),
    },
    "/chat"
  );
}

/** Shows a notification for a message in a thread that is not the open one. */
export async function showForegroundMessageNotification(
  alias: string,
  preview: string,
  /** Display label for the sender (lib/names.ts `peerLabel`); defaults to `@alias`. */
  label?: string
): Promise<void> {
  if (notificationPermission() !== "granted") return;
  await showNotification(
    `New message from ${label ?? `@${alias}`}`,
    {
      body: preview,
      icon: withBase("/icons/icon-192.png"),
      tag: `pager-thread-${alias}`,
    },
    `/chat/${encodeURIComponent(alias)}`
  );
}
