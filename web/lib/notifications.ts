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

/** Local-only check that permission and display work (the relay has no on-demand push). */
}

/** Shows a notification for a message in a thread that is not the open one. */
  if (notificationPermission() !== "granted") return;
}
