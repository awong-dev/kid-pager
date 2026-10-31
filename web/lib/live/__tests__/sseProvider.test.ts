/** `sseProvider.ts` against a fake `EventSource`: subscribe on open, merge, order/limit, decoding,
 * re-subscribe on reconnect. */

import { Timestamp } from "firebase/firestore";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveQuery } from "../types";

const posts: { path: string; body: { sub: string; query: unknown } }[] = [];
const dels: string[] = [];

vi.mock("../../api", () => {
  class ApiError extends Error {
    constructor(
      public status: number,
      detail: unknown
    ) {
      super(String(detail));
    }
  }
  return {
    ApiError,
    api: {
      post: vi.fn(async (path: string, body: { sub: string; query: unknown }) => {
        posts.push({ path, body });
        return { ok: true };
      }),
      del: vi.fn(async (path: string) => {
        dels.push(path);
      }),
    },
  };
});

class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static last: FakeEventSource | null = null;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  fireOpen() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
  drop() {
    this.readyState = FakeEventSource.CONNECTING;
    this.onerror?.();
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}

beforeEach(() => {
  posts.length = 0;
  dels.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("sseProvider", () => {
  it("subscribes on open, merges events, orders and limits locally", async () => {
    const { sseProvider } = await import("../sseProvider");
    const seen: { id: string; data: unknown }[][] = [];
    const q: LiveQuery = {
      collection: "messages",
      where: [["uids", "array-contains", "u1"]],
      orderBy: [["seq", "desc"]],
      limit: 2,
    };
    const unsub = sseProvider.subscribe(q, (docs) => seen.push(docs), () => undefined);
    const es = FakeEventSource.last!;
    expect(es.url).toMatch(/^\/api\/live\?stream=/);
    expect(posts).toHaveLength(0); // not before open
    es.fireOpen();
    await flush();
    expect(posts).toHaveLength(1);
    const sub = posts[0]!.body.sub;
    const doc = (id: string, seq: number) => ({ id, path: `messages/${id}`, data: { seq } });

    es.emit({ sub, type: "snapshot", docs: [doc("a", 1), doc("b", 2)] });
    expect(seen.at(-1)!.map((d) => d.id)).toEqual(["b", "a"]);
    es.emit({ sub, type: "added", ...doc("c", 3) });
    expect(seen.at(-1)!.map((d) => d.id)).toEqual(["c", "b"]);
    es.emit({ sub, type: "removed", id: "c", path: "messages/c", data: null });
    expect(seen.at(-1)!.map((d) => d.id)).toEqual(["b", "a"]);
    es.emit({ sub, type: "modified", ...doc("a", 9) });
    expect(seen.at(-1)!.map((d) => d.id)).toEqual(["a", "b"]);

    unsub();
    await flush();
    expect(dels).toEqual([`/live/${posts[0]!.path.split("/")[2]}/subscribe/${sub}`]);
    const before = seen.length;
    es.emit({ sub, type: "added", ...doc("d", 4) });
    expect(seen.length).toBe(before);
  });

  it("decodes timestamps and re-subscribes everything after a reconnect", async () => {
    const { sseProvider } = await import("../sseProvider");
    const got: unknown[] = [];
    sseProvider.subscribe({ doc: "settings/retention" }, (docs) => got.push(docs[0]?.data), () => undefined);
    sseProvider.subscribe({ collection: "users", where: [["familyId", "==", "f"]] }, () => undefined, () => undefined);
    const es = FakeEventSource.last!;
    es.fireOpen();
    await flush();
    expect(posts).toHaveLength(2);
    const sub = posts[0]!.body.sub;
    es.emit({
      sub,
      type: "snapshot",
      docs: [{ id: "retention", path: "settings/retention", data: { at: { $ts: 1_500_000 }, n: [{ $ts: 0 }] } }],
    });
    const data = got.at(-1) as { at: Timestamp; n: Timestamp[] };
    expect(data.at).toBeInstanceOf(Timestamp);
    expect(data.at.toMillis()).toBe(1500);
    expect(data.n[0]!.toMillis()).toBe(0);

    es.drop();
    es.fireOpen();
    await flush();
    expect(posts).toHaveLength(4);
    expect(posts.slice(2).map((p) => p.body.sub).sort()).toEqual(posts.slice(0, 2).map((p) => p.body.sub).sort());
  });

  it("reports a refused query to that subscription only", async () => {
    const apiMod = await import("../../api");
    const { sseProvider } = await import("../sseProvider");
    vi.mocked(apiMod.api.post).mockImplementationOnce(async () => {
      throw new apiMod.ApiError(403, "query not permitted");
    });
    const errors: Error[] = [];
    sseProvider.subscribe({ collection: "families" }, () => undefined, (e) => errors.push(e));
    FakeEventSource.last!.fireOpen();
    await flush();
    expect(errors).toHaveLength(1);
  });
});
