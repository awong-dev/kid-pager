 * `/settings/notifications`, never on load. */

import { getToken } from "firebase/messaging";
import { getMessagingIfSupported } from "./firebase-messaging";

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notificationPermission(): NotificationPermission | "unsupported" {
  if (!notificationsSupported()) return "unsupported";
  return Notification.permission;
}


  const messaging = await getMessagingIfSupported();
  if (!messaging) return null;
  const token = await getToken(messaging, {
    serviceWorkerRegistration: registration,
  });
  return token || null;
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
    registration = await navigator.serviceWorker.getRegistration(SW_PATH);
  }
  if (registration) {
    await registration.showNotification(title, { ...options, data: { url } });
    return;
  }
  const n = new Notification(title, options);
  n.onclick = () => {
    window.focus();
    // Full navigation, not `useRouter()` -- this runs outside React.
    window.location.href = url;
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
      icon: "/icons/icon-192.png",
    },
    "/chat"
  );
}

/** Shows a notification for a message in a thread that is not the open one. */
export async function showForegroundMessageNotification(
  alias: string,
): Promise<void> {
  if (notificationPermission() !== "granted") return;
  await showNotification(
    {
      body: preview,
      icon: "/icons/icon-192.png",
      tag: `pager-thread-${alias}`,
    },
    `/chat/${encodeURIComponent(alias)}`
  );
}
