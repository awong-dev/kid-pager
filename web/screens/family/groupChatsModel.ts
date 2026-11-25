/** Pure helpers for the "Group chats" section of `/family/people`: one summary row per member who
 * owns a bridge phone. Bridge phones are server-only data, so the rows come from
 * `GET /api/family/bridges` and, for the count, `GET /api/family/members/{uid}/chat`. */

import type { BridgeRow } from "@/lib/types";

export interface GroupOwner {
  uid: string;
  name: string;
  alias: string | null;
  /** All of this member's bridge phones. */
  phones: number;
  /** Phones that are paired (only these can carry conversations). */
  paired: number;
}

/** One row per owner, named by `ownerName` then `ownerAlias`, sorted by name. */
export function groupOwners(bridges: readonly Pick<BridgeRow, "ownerUid" | "ownerName" | "ownerAlias" | "paired">[]): GroupOwner[] {
  const byUid = new Map<string, GroupOwner>();
  for (const b of bridges) {
    const row = byUid.get(b.ownerUid) ?? {
      uid: b.ownerUid,
      name: b.ownerName || (b.ownerAlias ? `@${b.ownerAlias}` : b.ownerUid),
      alias: b.ownerAlias,
      phones: 0,
      paired: 0,
    };
    row.phones += 1;
    if (b.paired) row.paired += 1;
    byUid.set(b.ownerUid, row);
  }
  return [...byUid.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/** "3 subscribed" / "1 subscribed" / "Nothing subscribed yet" / "Phone not paired yet"; `count`
 * null means unknown (the request failed or is pending), which shows nothing. */
export function subscribedLine(owner: Pick<GroupOwner, "paired">, count: number | null): string | null {
  if (owner.paired === 0) return "Phone not paired yet";
  if (count === null) return null;
  return count === 0 ? "Nothing subscribed yet" : `${count} subscribed`;
}
