/**
 * Home provider: one `EventSource` per tab (browsers cap HTTP/1.1 connections per origin at
 * six); queries are registered with `POST /api/live/{stream}/subscribe` and dropped with `DELETE`.
 */

import { Bytes, Timestamp } from "firebase/firestore";

import { ApiError, api } from "../api";
import { withBase } from "../basePath";
import { type LiveDoc, type LiveProvider, type LiveQuery, isDocQuery } from "./types";

interface WireDoc {
  id: string;
  path: string;
  data: unknown;
}

type WireEvent =
  | { sub: string; type: "snapshot"; docs: WireDoc[] }
  | ({ sub: string; type: "added" | "modified" | "removed" } & WireDoc);

interface Sub {
  q: LiveQuery;
  onNext: (docs: LiveDoc<unknown>[]) => void;
  onError: (err: Error) => void;
  docs: Map<string, LiveDoc<unknown>>;
}

/** The store's JSON encoding back to Firestore types. */
export function decodeValue(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(decodeValue);
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    const keys = Object.keys(o);
    if (keys.length === 1 && keys[0] === "$ts" && typeof o.$ts === "number") {
      const us = o.$ts;
      const seconds = Math.floor(us / 1_000_000);
      return new Timestamp(seconds, (us - seconds * 1_000_000) * 1000);
    }
    if (keys.length === 1 && keys[0] === "$b64" && typeof o.$b64 === "string") {
      return Bytes.fromBase64String(o.$b64);
    }
    const out: Record<string, unknown> = {};
    for (const k of keys) out[k] = decodeValue(o[k]);
    return out;
  }
  return v;
}

function fieldOf(data: unknown, dotted: string): unknown {
  let cur: unknown = data;
  for (const part of dotted.split(".")) {
    if (!cur || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

// Firestore's cross-type order for the types the app orders by.
function typeRank(v: unknown): number {
  if (v === null || v === undefined) return 0;
  if (typeof v === "boolean") return 1;
  if (typeof v === "number") return 2;
  if (v instanceof Timestamp) return 3;
  if (typeof v === "string") return 4;
  return 5;
}

function compareValues(a: unknown, b: unknown): number {
  const ra = typeRank(a);
  const rb = typeRank(b);
  if (ra !== rb) return ra - rb;
  if (a instanceof Timestamp && b instanceof Timestamp) {
    return a.seconds - b.seconds || a.nanoseconds - b.nanoseconds;
  }
  if (typeof a === "number" || typeof a === "boolean") return Number(a) - Number(b);
  if (typeof a === "string") return a < (b as string) ? -1 : a > (b as string) ? 1 : 0;
  return 0;
}

/** Client-side `orderBy` (document path is the implicit last key, as in Firestore) and `limit`. */
export function shape(q: LiveQuery, docs: LiveDoc<unknown>[]): LiveDoc<unknown>[] {
  if (isDocQuery(q)) return docs;
  const order = q.orderBy ?? [];
  const lastDesc = order.length > 0 && order[order.length - 1]![1] === "desc";
  const sorted = [...docs].sort((x, y) => {
    for (const [field, dir] of order) {
      const c = compareValues(fieldOf(x.data, field), fieldOf(y.data, field));
      if (c !== 0) return dir === "desc" ? -c : c;
    }
    const c = x.path < y.path ? -1 : x.path > y.path ? 1 : 0;
    return lastDesc ? -c : c;
  });
  return q.limit !== undefined ? sorted.slice(0, q.limit) : sorted;
}

function toDoc(w: WireDoc): LiveDoc<unknown> {
  return { id: w.id, path: w.path, data: decodeValue(w.data) };
}

const RECREATE_MS = 5000;
const IDLE_CLOSE_MS = 2000;

class LiveHub {
  private es: EventSource | null = null;
  private streamId = "";
  private open = false;
  private nextSub = 0;
  private readonly subs = new Map<string, Sub>();
  private recreateTimer: ReturnType<typeof setTimeout> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;

  subscribe(q: LiveQuery, onNext: Sub["onNext"], onError: Sub["onError"]): () => void {
    const id = `s${++this.nextSub}`;
    const sub: Sub = { q, onNext, onError, docs: new Map() };
    this.subs.set(id, sub);
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
    this.ensureStream();
    if (this.open) this.send(id, sub);
    return () => {
      if (this.subs.get(id) !== sub) return;
      this.subs.delete(id);
      if (this.open) {
        api.del(`/live/${this.streamId}/subscribe/${id}`).catch(() => undefined);
      }
      // A query change unsubscribes then resubscribes at once; keep the stream.
      if (this.subs.size === 0 && !this.idleTimer) {
        this.idleTimer = setTimeout(() => {
          this.idleTimer = null;
          if (this.subs.size === 0) this.close();
        }, IDLE_CLOSE_MS);
      }
    };
  }

  private ensureStream(): void {
    if (this.es || this.recreateTimer) return;
    this.streamId = crypto.randomUUID();
    const es = new EventSource(withBase(`/api/live?stream=${this.streamId}`));
    this.es = es;
    es.onopen = () => {
      this.open = true;
      for (const [id, sub] of this.subs) this.send(id, sub);
    };
    es.onmessage = (ev: MessageEvent<string>) => this.handle(JSON.parse(ev.data) as WireEvent);
    es.onerror = () => {
      this.open = false;
      // CONNECTING: the browser reconnects and `onopen` re-subscribes. CLOSED (e.g. 401): new one.
      if (es.readyState === EventSource.CLOSED) {
        this.es = null;
        this.recreateTimer = setTimeout(() => {
          this.recreateTimer = null;
          if (this.subs.size > 0) this.ensureStream();
        }, RECREATE_MS);
      }
    };
  }

  private close(): void {
    this.es?.close();
    this.es = null;
    this.open = false;
  }

  private send(id: string, sub: Sub): void {
    const streamId = this.streamId;
    api.post(`/live/${streamId}/subscribe`, { sub: id, query: sub.q }).catch((err: unknown) => {
      if (this.subs.get(id) !== sub || streamId !== this.streamId) return;
      // 404: the stream dropped; the next `open` re-sends. Anything else (403) is final.
      if (err instanceof ApiError && err.status === 404) return;
      this.subs.delete(id);
      sub.onError(err instanceof Error ? err : new Error(String(err)));
    });
  }

  private handle(ev: WireEvent): void {
    const sub = this.subs.get(ev.sub);
    if (!sub) return;
    if (ev.type === "snapshot") {
      sub.docs = new Map(ev.docs.map((w) => [w.path, toDoc(w)]));
    } else if (ev.type === "removed") {
      sub.docs.delete(ev.path);
    } else {
      sub.docs.set(ev.path, toDoc(ev));
    }
    sub.onNext(shape(sub.q, [...sub.docs.values()]));
  }
}

let hub: LiveHub | null = null;

export const sseProvider: LiveProvider = {
  subscribe(q, onNext, onError) {
    hub ??= new LiveHub();
    return hub.subscribe(q, onNext, onError);
  },
};
