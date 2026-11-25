/** The Home launcher's cards as data (order = display order), plus the pure helpers that turn live
 * data into each card's status line. Adding or renaming a card is an edit to `CARDS`. */

import type { DeviceDoc } from "@/lib/types";

export type CardId =
  | "messages" | "alerts" | "people" | "pagers" | "settings" | "system" | "myPager" | "notifications";

export type IconKey =
  | "forum" | "alerts" | "groups" | "remote" | "settings" | "admin" | "notifications";

/** Who sees a card: `everyone`, `admin` (family admin or super), `super`, or `members`
 * (signed in but NOT an admin). */
export type Audience = "everyone" | "admin" | "super" | "members";

export interface HomeCard {
  id: CardId;
  title: string;
  description: string;
  href: string;
  icon: IconKey;
  audience: Audience;
  /** Needs a family in scope; a super with none chosen sees "Choose a family" as its status. */
  needsFamily?: boolean;
  featured?: boolean;
}

export const CARDS: readonly HomeCard[] = [
  { id: "messages", title: "Messages", href: "/chat", icon: "forum", audience: "everyone", featured: true,
    description: "Message your family's pagers and see where they are." },
  { id: "alerts", title: "Alerts", href: "/alerts", icon: "alerts", audience: "admin", needsFamily: true,
    description: "Requests waiting for your OK." },
  { id: "people", title: "People & contacts", href: "/family/people", icon: "groups", audience: "admin", needsFamily: true,
    description: "Everyone your pagers can message." },
  { id: "pagers", title: "Pagers", href: "/family/devices", icon: "remote", audience: "admin", needsFamily: true,
    description: "Set up and look after your family's pagers." },
  { id: "settings", title: "Family settings", href: "/family/settings", icon: "settings", audience: "admin",
    description: "Your family's name, pager notices and how long history is kept." },
  { id: "system", title: "System", href: "/admin/families", icon: "admin", audience: "super",
    description: "Families, users and server settings." },
  { id: "myPager", title: "My pager", href: "/account/pagers", icon: "remote", audience: "members",
    description: "Battery and status of your pager." },
  { id: "notifications", title: "Notifications", href: "/account/notifications", icon: "notifications",
    audience: "members", description: "Get told when a message arrives." },
];

export function cardVisible(c: HomeCard, v: { isFamilyAdmin: boolean; isSuper: boolean }): boolean {
  switch (c.audience) {
    case "everyone":
      return true;
    case "admin":
      return v.isFamilyAdmin;
    case "super":
      return v.isSuper;
    case "members":
      return !v.isFamilyAdmin;
  }
}

/** A card's status line. */
export type Status =
  | { kind: "loading" }
  | { kind: "none" }
  | { kind: "text"; text: string; second?: string }
  | { kind: "alerts"; count: number };

export const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

export type DeviceLike = Pick<DeviceDoc, "revokedAt" | "status">;

export const activeDevices = <T extends Pick<DeviceDoc, "revokedAt">>(ds: readonly T[]): T[] =>
  ds.filter((d) => !d.revokedAt);

export const isOnline = (d: DeviceLike): boolean => d.status.state === "online";

export function unreadStatus(unread: number): string {
  return unread > 0 ? `${unread} unread` : "All caught up";
}

/** "2 of 3 pagers online"; null when there are no pagers. */
export function onlineLine(ds: readonly DeviceLike[]): string | null {
  const live = activeDevices(ds);
  if (live.length === 0) return null;
  return `${live.filter(isOnline).length} of ${plural(live.length, "pager")} online`;
}

/** "3 pagers, 1 offline" / "3 pagers" / "No pagers yet". */
export function pagersStatus(ds: readonly DeviceLike[]): string {
  const live = activeDevices(ds);
  if (live.length === 0) return "No pagers yet";
  const offline = live.filter((d) => !isOnline(d)).length;
  return offline > 0 ? `${plural(live.length, "pager")}, ${offline} offline` : plural(live.length, "pager");
}

/** "Online" / "Last seen 2h ago" / "Offline" for one pager; `ago` formats a ms timestamp. */
export function myPagerStatus(
  ds: readonly DeviceLike[],
  lastSeenMs: (d: DeviceLike) => number | null,
  ago: (ms: number) => string,
): string {
  const live = activeDevices(ds);
  if (live.length === 0) return "No pager linked";
  if (live.some(isOnline)) return "Online";
  const seen = live.map(lastSeenMs).filter((m): m is number => m !== null);
  return seen.length > 0 ? `Last seen ${ago(Math.max(...seen))}` : "Offline";
}
