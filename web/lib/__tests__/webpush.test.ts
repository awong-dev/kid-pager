/** `ensurePushSubscription`: Web Push with the relay's VAPID key, FCM migration, FCM fallback. */

import { describe, expect, it, vi } from "vitest";

import { type PushDeps, ensurePushSubscription, sameKey, urlBase64ToBytes } from "../webpush";

const RELAY_KEY = "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM";
const FCM_KEY = "BDOU99-h67HcA6JeFXHbSNMu7e2yNNu3RzoMj8TM4W88jITfq7ZmPvIM1Iv-4_l2LxQcYwhqby2xGpWwzjfAnG4";

function fakeSub(key: string, endpoint: string, log: string[]): PushSubscription {
  return {
    endpoint,
    options: { applicationServerKey: urlBase64ToBytes(key).buffer, userVisibleOnly: true },
    toJSON: () => ({ endpoint, keys: { p256dh: `p-${endpoint}`, auth: `a-${endpoint}` } }),
    unsubscribe: async () => {
      log.push(`unsubscribe ${endpoint}`);
      return true;
    },
  } as unknown as PushSubscription;
}

function deps(over: Partial<PushDeps> & { existing?: PushSubscription | null; log: string[] }): PushDeps {
  const { existing = null, log, ...rest } = over;
  return {
    backend: "firebase",
    pushManager: {
      getSubscription: async () => existing,
      subscribe: async (opts?: PushSubscriptionOptionsInit) => {
        const key = opts?.applicationServerKey as Uint8Array;
        log.push(`subscribe ${sameKey(key.buffer as ArrayBuffer, urlBase64ToBytes(RELAY_KEY)) ? "relay" : "other"}`);
        return fakeSub(RELAY_KEY, "https://push.example/new", log);
      },
    },
    getVapidKey: async () => RELAY_KEY,
    putSubscription: async (body) => {
      log.push(`put ${body.endpoint} ${body.keys.p256dh} ${body.ua}`);
    },
    getFcmToken: async () => {
      log.push("getFcmToken");
      return "fcm-tok";
    },
    postFcmToken: async (t) => {
      log.push(`post ${t}`);
    },
    deleteFcmToken: async (t) => {
      log.push(`delete ${t}`);
    },
    userAgent: "UA",
    ...rest,
  };
}

describe("urlBase64ToBytes", () => {
  it("decodes an uncompressed P-256 point", () => {
    const bytes = urlBase64ToBytes(RELAY_KEY);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(4);
    expect(sameKey(bytes.buffer, urlBase64ToBytes(RELAY_KEY))).toBe(true);
    expect(sameKey(bytes.buffer, urlBase64ToBytes(FCM_KEY))).toBe(false);
    expect(sameKey(null, bytes)).toBe(false);
  });
});

describe("ensurePushSubscription", () => {
  it("subscribes with the relay key and PUTs the subscription", async () => {
    const log: string[] = [];
    expect(await ensurePushSubscription(deps({ log, backend: "home" }))).toBe("webpush");
    expect(log).toEqual(["subscribe relay", "put https://push.example/new p-https://push.example/new UA"]);
  });

  it("keeps a subscription that already uses the relay key", async () => {
    const log: string[] = [];
    const existing = fakeSub(RELAY_KEY, "https://push.example/kept", log);
    expect(await ensurePushSubscription(deps({ log, existing }))).toBe("webpush");
    expect(log).toEqual(["put https://push.example/kept p-https://push.example/kept UA"]);
  });

  it("migrates an FCM subscription silently (firebase build)", async () => {
    const log: string[] = [];
    const existing = fakeSub(FCM_KEY, "https://fcm.googleapis.com/fcm/send/old", log);
    expect(await ensurePushSubscription(deps({ log, existing }))).toBe("webpush");
    expect(log).toEqual([
      "getFcmToken",
      "unsubscribe https://fcm.googleapis.com/fcm/send/old",
      "subscribe relay",
      "put https://push.example/new p-https://push.example/new UA",
      "delete fcm-tok",
    ]);
  });

  it("still migrates when the old FCM token cannot be named", async () => {
    const log: string[] = [];
    const existing = fakeSub(FCM_KEY, "https://fcm.googleapis.com/fcm/send/old", log);
    const getFcmToken = vi.fn(async () => {
      throw new Error("no token");
    });
    expect(await ensurePushSubscription(deps({ log, existing, getFcmToken }))).toBe("webpush");
    expect(log).toEqual([
      "unsubscribe https://fcm.googleapis.com/fcm/send/old",
      "subscribe relay",
      "put https://push.example/new p-https://push.example/new UA",
    ]);
  });

  it("keeps today's FCM token path when the relay has no VAPID key", async () => {
    const log: string[] = [];
    const existing = fakeSub(FCM_KEY, "https://fcm.googleapis.com/fcm/send/old", log);
    const got = await ensurePushSubscription(deps({ log, existing, getVapidKey: async () => null }));
    expect(got).toBe("fcm");
    expect(log).toEqual(["getFcmToken", "post fcm-tok"]);
  });

  it("has no push at home without a VAPID key", async () => {
    const log: string[] = [];
    const got = await ensurePushSubscription(deps({ log, backend: "home", getVapidKey: async () => null }));
    expect(got).toBeNull();
    expect(log).toEqual([]);
  });
});
