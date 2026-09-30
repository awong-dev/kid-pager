import type { Timestamp } from "firebase/firestore";

import { LOC_REQ_TTL_MS, type MessageDoc } from "./types";

/** Epoch ms, or `null` while a pending server timestamp is unresolved. */
export function tsToMillis(ts: Timestamp | null | undefined): number | null {
  if (!ts) return null;
  return ts.toMillis();
}

export function formatClock(ms: number | null): string {
  if (ms === null) return "";
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function formatRelativeAge(ms: number): string {
  const deltaS = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (deltaS < 60) return `${deltaS}s ago`;
  const deltaM = Math.round(deltaS / 60);
  if (deltaM < 60) return `${deltaM}m ago`;
  const deltaH = Math.round(deltaM / 60);
  if (deltaH < 24) return `${deltaH}h ago`;
  const deltaD = Math.round(deltaH / 24);
  return `${deltaD}d ago`;
}

/** Day heading in the viewer's local time zone: "Today", "Yesterday", or a short date. */
export function formatDay(ms: number, nowMs: number = Date.now()): string {
  const d = new Date(ms);
  const now = new Date(nowMs);
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/** A `loc_req` still `sent` 15 minutes after `createdAt` is expired (derived, not stored;
 * docs/PROTOCOL.md §13.4). */
export function isLocReqExpired(msg: Pick<MessageDoc, "kind" | "createdAt">): boolean {
  if (msg.kind !== "loc_req") return false;
  const createdMs = tsToMillis(msg.createdAt);
  if (createdMs === null) return false;
  return Date.now() - createdMs > LOC_REQ_TTL_MS;
}
