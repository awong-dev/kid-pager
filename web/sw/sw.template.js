// The app's service worker template; `scripts/gen-sw.mjs` writes `public/sw.js` from it.
// The FIREBASE-BEGIN/END block (FCM compat scripts) is dropped in the home build.

// URL prefix this worker is served under, read at runtime from its own scope (`/pager/` gives
// `/pager`; `/` gives ""). The relay may host the app under a prefix it picks at startup, so
// nothing here is baked in at build time. Payload `url`s and asset paths are logical (`/chat`).
const BASE = new URL(self.registration.scope).pathname.replace(/\/+$/, "");
function withBase(path) {
  if (path.charAt(0) !== "/" || path.charAt(1) === "/") return path;
  // A url that already carries the prefix is left alone.
  if (BASE && (path === BASE || path.indexOf(BASE + "/") === 0)) return path;
  return BASE + path;
}
const ICON = () => withBase("/icons/icon-192.png");

// Payload values are all strings: `kind` (`message` | `geofence` | `alert`), `convKey`, `id`,
// `title`, `body`, `url` (path to open). FCM delivers it as `payload.data`; Web Push as the body.
function showPagerNotification(data) {
  const title = data.title || "Pager";
  const body = data.body || "";
  const url = withBase(data.url || "/chat");

  // The kinds differ only in the `tag` that collapses notifications of one kind.
  let options;
  switch (data.kind) {
    case "message":
      options = {
        body,
        icon: ICON(),
        tag: data.convKey ? `pager-thread-${data.convKey}` : undefined,
        data: { url },
      };
      break;
    case "geofence":
      options = {
        body,
        icon: ICON(),
        tag: data.id ? `pager-geofence-${data.id}` : undefined,
        data: { url },
      };
      break;
    case "alert":
      options = {
        body,
        icon: ICON(),
        tag: data.id ? `pager-alert-${data.id}` : undefined,
        data: { url },
      };
      break;
    default:
      options = { body, icon: ICON(), data: { url } };
      break;
  }

  return self.registration.showNotification(title, options);
}

// FIREBASE-BEGIN
importScripts("https://www.gstatic.com/firebasejs/12.3.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/12.3.0/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "__NEXT_PUBLIC_FIREBASE_API_KEY__",
  authDomain: "__NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN__",
  projectId: "__NEXT_PUBLIC_FIREBASE_PROJECT_ID__",
  storageBucket: "__NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET__",
  messagingSenderId: "__NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID__",
  appId: "__NEXT_PUBLIC_FIREBASE_APP_ID__",
});

// The compat SDK also hands raw Web Push payloads here (no `payload.data`); the `push`
// listener below shows those, so only an FCM data message is shown from this callback.
firebase.messaging().onBackgroundMessage((payload) => {
  if (payload.data && payload.data.kind) {
    return showPagerNotification(payload.data);
  }
  return undefined;
});
// FIREBASE-END

// Web Push from the relay: the body is the payload as one JSON object. An FCM push (no
// top-level `kind`) is left to `onBackgroundMessage`; a visible window shows nothing here.
self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    data = null;
  }
  if (!data || typeof data.kind !== "string") return;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      if (clientList.some((c) => c.visibilityState === "visible")) return undefined;
      return showPagerNotification(data);
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = withBase((event.notification.data && event.notification.data.url) || "/chat");
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      // Prefer a client already on the right page.
      for (const client of clientList) {
        if ("focus" in client && new URL(client.url).pathname === url) {
          return client.focus();
        }
      }
      // Otherwise navigate and focus the first open tab.
      const [client] = clientList;
      if (client && "focus" in client) {
        if ("navigate" in client) {
          return client.navigate(url).then(() => client.focus());
        }
        return client.focus();
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(url);
      }
      return undefined;
    })
  );
});
