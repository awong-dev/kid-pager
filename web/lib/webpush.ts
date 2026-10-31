/** Web Push subscription and migration from FCM; every browser and relay call is injected. */

export type PushPath = "webpush" | "fcm";

export interface SubscriptionBody {
  endpoint: string;
  keys: { p256dh: string; auth: string };
  ua: string;
}

export interface PushDeps {
  backend: "firebase" | "home";
  pushManager: Pick<PushManager, "getSubscription" | "subscribe">;
  /** The relay's VAPID public key (base64url), `null` when not configured. */
  getVapidKey(): Promise<string | null>;
  putSubscription(body: SubscriptionBody): Promise<void>;
  /** FCM SDK `getToken()` against the same registration (firebase only). */
  getFcmToken(): Promise<string | null>;
  postFcmToken(token: string): Promise<void>;
  deleteFcmToken(token: string): Promise<void>;
  userAgent: string;
}

export function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function sameKey(a: ArrayBuffer | null | undefined, b: Uint8Array): boolean {
  if (!a) return false;
  const x = new Uint8Array(a);
  return x.length === b.length && x.every((v, i) => v === b[i]);
}

function subscriptionBody(sub: PushSubscription, ua: string): SubscriptionBody {
  const json = sub.toJSON();
  return {
    endpoint: sub.endpoint,
    keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" },
    ua,
  };
}

export async function ensurePushSubscription(deps: PushDeps): Promise<PushPath | null> {
  const vapidKey = await deps.getVapidKey();
  if (!vapidKey) {
    if (deps.backend === "home") return null;
    const token = await deps.getFcmToken();
    if (!token) return null;
    await deps.postFcmToken(token);
    return "fcm";
  }

  const key = urlBase64ToBytes(vapidKey);
  let sub = await deps.pushManager.getSubscription();
  let oldFcmToken: string | null = null;
  if (sub && !sameKey(sub.options.applicationServerKey, key)) {
    if (deps.backend === "firebase") {
      // The subscription is still FCM's, so the SDK returns its token; naming it is best-effort.
      oldFcmToken = await deps.getFcmToken().catch(() => null);
    }
    await sub.unsubscribe();
    sub = null;
  }
  if (!sub) {
    sub = await deps.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  }
  await deps.putSubscription(subscriptionBody(sub, deps.userAgent));
  if (oldFcmToken) await deps.deleteFcmToken(oldFcmToken);
  return "webpush";
}
