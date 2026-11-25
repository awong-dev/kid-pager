import { describe, expect, it } from "vitest";

import type { DirectoryEntry } from "@/lib/directory";
import type { ConversationDoc } from "@/lib/types";

import {
  AUDIT_COLLAPSE_OVER,
  auditRows,
  collapseAudit,
  conversationRows,
  fixLine,
  pagerLine,
  personTitle,
  sortCards,
} from "../messagesModel";

const ts = (ms: number) => ({ toMillis: () => ms }) as never;
const conv = (o: Partial<ConversationDoc>): ConversationDoc =>
  ({ kind: "dm", uids: [], lastPreview: "", lastMessageAt: null, ...o }) as ConversationDoc;
const dir: Record<string, DirectoryEntry> = {
  me: { uid: "me", alias: "mum", displayName: "Mum", kind: "person", familyId: "f", role: "admin" },
  kid: { uid: "kid", alias: "kid", displayName: "Kid", kind: "person", familyId: "f", role: "member" },
  out: { uid: "out", alias: "gran", displayName: "Gran", kind: "person", familyId: "g", role: "member" },
};
const byUid = (u: string) => dir[u];

describe("conversationRows", () => {
  it("sorts newest first, names DMs from the directory and routes groups by alias", () => {
    const rows = conversationRows(
      [
        { id: "a", data: conv({ uids: ["me", "kid"], lastMessageAt: ts(1), unread: { me: 2 } }) },
        { id: "b", data: conv({ kind: "group", uids: ["me"], name: "Fam", alias: "fam", lastMessageAt: ts(5) }) },
      ],
      "me",
      byUid,
    );
    expect(rows.map((r) => r.convKey)).toEqual(["b", "a"]);
    expect(rows[0]).toMatchObject({ label: "Fam", routeAlias: "fam", isGroup: true, groupAlias: "fam" });
    expect(rows[1]).toMatchObject({ label: "@kid", routeAlias: "kid", unread: 2 });
  });
  it("drops a DM whose peer cannot be named", () => {
    expect(conversationRows([{ id: "x", data: conv({ uids: ["me", "nobody"] }) }], "me", byUid)).toEqual([]);
  });
});

describe("auditRows", () => {
  it("skips my own conversations and labels kid to peer", () => {
    const rows = auditRows(
      [
        { id: "mine", data: conv({ uids: ["me", "kid"], lastMessageAt: ts(9) }) },
        { id: "k", data: conv({ uids: ["out", "kid"], lastMessageAt: ts(1) }) },
      ],
      "me",
      "f",
      byUid,
    );
    expect(rows.map((r) => r.convKey)).toEqual(["k"]);
    expect(rows[0]!.label).toBe("@kid ↔ @gran");
  });
});

describe("collapseAudit", () => {
  const rows = (n: number) => Array.from({ length: n }, (_, i) => i);
  it("keeps short lists whole", () => {
    expect(collapseAudit(rows(AUDIT_COLLAPSE_OVER), false)).toEqual({ shown: rows(AUDIT_COLLAPSE_OVER), hidden: 0 });
  });
  it("tucks the rest of a long list away until expanded", () => {
    const long = rows(AUDIT_COLLAPSE_OVER + 3);
    expect(collapseAudit(long, false)).toMatchObject({ hidden: 3 });
    expect(collapseAudit(long, false).shown).toHaveLength(AUDIT_COLLAPSE_OVER);
    expect(collapseAudit(long, true)).toEqual({ shown: long, hidden: 0 });
  });
});

describe("location card text", () => {
  it("personTitle", () => {
    expect(personTitle({ isOwn: true, displayName: "Me", pagerName: "p" })).toBe("You");
    expect(personTitle({ isOwn: false, displayName: " Kid ", alias: "kid", pagerName: "p" })).toBe("Kid");
    expect(personTitle({ isOwn: false, alias: "kid", pagerName: "p" })).toBe("@kid");
    expect(personTitle({ isOwn: false, pagerName: "pgr-1" })).toBe("pgr-1");
  });
  it("sortCards puts the viewer first, then A to Z", () => {
    const out = sortCards([
      { isOwn: false, title: "Zed" },
      { isOwn: false, title: "Amy" },
      { isOwn: true, title: "You" },
    ]);
    expect(out.map((c) => c.title)).toEqual(["You", "Amy", "Zed"]);
  });
  it("pagerLine", () => {
    const ago = (ms: number) => `${ms}ago`;
    expect(pagerLine({ state: "online", updatedAt: null }, ago)).toEqual({ online: true, text: "Online" });
    expect(pagerLine({ state: "offline", updatedAt: ts(7) }, ago).text).toBe("Offline, last seen 7ago");
    expect(pagerLine({ state: null, updatedAt: null }, ago)).toEqual({ online: false, text: "Offline" });
  });
  it("fixLine says how the position was found and flags a stale one", () => {
    const ago = () => "5m ago";
    const now = 1_000_000_000;
    const fresh = fixLine({ ts: now / 1000 - 60, src: "gnss", accM: 10 }, ago, now);
    expect(fresh).toEqual({ text: "Position 5m ago (GPS)", stale: false });
    const old = fixLine({ ts: now / 1000 - 7200, lastTs: now / 1000 - 3600, src: "cell", accM: 900 }, ago, now);
    expect(old).toEqual({ text: "Position 5m ago (approximate, cell tower)", stale: true });
  });
});
