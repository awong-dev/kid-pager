/** Pure helpers for the Messages dashboard (`/chat`): conversation rows, the read-only audit rows,
 * and the text on a "Where they are" card. No React, so `__tests__/messagesModel.test.ts` covers them. */

import type { DirectoryEntry } from "@/lib/directory";
import { peerLabel } from "@/lib/names";
import { isCoarseFix, isStaleFix, sourceLabel } from "@/lib/location";
import { tsToMillis } from "@/lib/time";
import type { ConversationDoc, DeviceDoc, LocationFixDoc } from "@/lib/types";

type ByUid = (uid: string) => DirectoryEntry | undefined;
type Doc<T> = { id: string; data: T };

/** The audit list is collapsed behind "Show all" when it has more rows than this. */
export const AUDIT_COLLAPSE_OVER = 8;

/** An external's number as digits (no `+`); its alias is a hash, so only `phone` carries it. */
export function externalDigits(phone: string | null | undefined): string | undefined {
  return phone ? phone.replace(/^\+/, "") : undefined;
}

const lastMs = (c: Pick<ConversationDoc, "lastMessageAt">): number => tsToMillis(c.lastMessageAt) ?? 0;

/** A row of "Conversations": my own conversation, named and routed. */
export interface ConversationRow {
  convKey: string;
  label: string;
  /** What `/chat/:alias` takes: the peer's alias, or the group's own alias. */
  routeAlias: string;
  isGroup: boolean;
  /** Group alias for "leave"; absent for a DM. */
  groupAlias?: string;
  bridge: boolean;
  phone?: string;
  unread: number;
  lastPreview: string;
  lastMessageAt: ConversationDoc["lastMessageAt"];
}

/** My conversations, newest first. A DM is named from the conversation's `participants`, falling
 * back to the directory; a DM with neither is dropped. A group routes by its own `alias`. */
export function conversationRows(docs: readonly Doc<ConversationDoc>[], myUid: string, byUid: ByUid): ConversationRow[] {
  const rows: ConversationRow[] = [];
  for (const { id, data: c } of [...docs].sort((a, b) => lastMs(b.data) - lastMs(a.data))) {
    const base = {
      convKey: id,
      bridge: Boolean(c.bridge),
      unread: c.unread?.[myUid] ?? 0,
      lastPreview: c.lastPreview,
      lastMessageAt: c.lastMessageAt,
    };
    if (c.kind === "group") {
      rows.push({
        ...base,
        label: c.name ?? c.alias ?? id,
        routeAlias: c.alias ?? id,
        isGroup: true,
        groupAlias: c.alias,
      });
      continue;
    }
    const peerUid = c.uids.find((u) => u !== myUid) ?? c.uids[0] ?? null;
    const participant = peerUid ? c.participants?.[peerUid] : undefined;
    const entry = peerUid ? byUid(peerUid) : undefined;
    const alias = participant?.alias ?? entry?.alias;
    if (!alias) continue;
    const kind = participant?.kind ?? entry?.kind;
    rows.push({
      ...base,
      label: peerLabel({ alias, displayName: participant?.displayName || entry?.displayName, kind }),
      routeAlias: alias,
      isGroup: false,
      phone: kind === "external" ? externalDigits(participant?.phone ?? entry?.phone) : undefined,
    });
  }
  return rows;
}

/** A row of "All family conversations": one this admin is not a party to. */
export interface AuditRow {
  convKey: string;
  label: string;
  /** E.164 digits of an SMS-external party, shown under the label. */
  phone?: string;
  lastPreview: string;
  lastMessageAt: ConversationDoc["lastMessageAt"];
}

/** Conversations of the family that `myUid` is not in, newest first, labelled "@kid <-> peer" (or
 * the group name); `byUid` only tells which uid is in the family. */
export function auditRows(
  docs: readonly Doc<ConversationDoc>[],
  myUid: string,
  familyId: string | null,
  byUid: ByUid,
): AuditRow[] {
  return docs
    .filter((d) => !d.data.uids.includes(myUid))
    .sort((a, b) => lastMs(b.data) - lastMs(a.data))
    .map(({ id, data: c }): AuditRow => {
      const common = { convKey: id, lastPreview: c.lastPreview, lastMessageAt: c.lastMessageAt };
      if (c.kind === "group") return { ...common, label: c.name ?? c.alias ?? id };
      const participants = c.participants ?? {};
      const kidUid = c.uids.find((u) => byUid(u)?.familyId === familyId) ?? c.uids[0];
      const peerUid = c.uids.find((u) => u !== kidUid);
      const kidAlias = (kidUid && (participants[kidUid]?.alias ?? byUid(kidUid)?.alias)) ?? "?";
      const peer = peerUid ? participants[peerUid] : undefined;
      const peerDir = peerUid ? byUid(peerUid) : undefined;
      const peerText = peerLabel({
        alias: peer?.alias ?? peerDir?.alias ?? "?",
        displayName: peer?.displayName || peerDir?.displayName,
        kind: peer?.kind ?? peerDir?.kind,
      });
      return {
        ...common,
        label: `@${kidAlias} ↔ ${peerText}`,
        phone: peer?.kind === "external" ? externalDigits(peer.phone ?? peerDir?.phone) : undefined,
      };
    });
}

/** The rows to show now and how many are tucked behind "Show all". */
export function collapseAudit<T>(rows: readonly T[], expanded: boolean): { shown: T[]; hidden: number } {
  if (expanded || rows.length <= AUDIT_COLLAPSE_OVER) return { shown: [...rows], hidden: 0 };
  return { shown: rows.slice(0, AUDIT_COLLAPSE_OVER), hidden: rows.length - AUDIT_COLLAPSE_OVER };
}

// --- "Where they are" cards ---------------------------------------------------------------

/** The card's heading: "You", the owner's name, "@alias", or the pager's name as a last resort. */
export function personTitle(p: {
  isOwn: boolean;
  displayName?: string | null;
  alias?: string;
  pagerName: string;
}): string {
  if (p.isOwn) return "You";
  return p.displayName?.trim() || (p.alias ? `@${p.alias}` : p.pagerName);
}

/** Own pager first, then A to Z by title. */
export function sortCards<T extends { isOwn: boolean; title: string }>(cards: readonly T[]): T[] {
  return [...cards].sort((a, b) => Number(b.isOwn) - Number(a.isOwn) || a.title.localeCompare(b.title));
}

/** "Online", or "Offline" plus when it was last heard from, if known. `ago` formats a ms time. */
export function pagerLine(
  status: Pick<DeviceDoc["status"], "state" | "updatedAt">,
  ago: (ms: number) => string,
): { online: boolean; text: string } {
  if (status.state === "online") return { online: true, text: "Online" };
  const seen = tsToMillis(status.updatedAt);
  return { online: false, text: seen !== null ? `Offline, last seen ${ago(seen)}` : "Offline" };
}

/** The last known position as a sentence: "Last seen 5m ago (GPS)", "...approximate, cell tower". */
export function fixLine(
  fix: Pick<LocationFixDoc, "ts" | "lastTs" | "src" | "accM">,
  ago: (ms: number) => string,
  nowMs: number = Date.now(),
): { text: string; stale: boolean } {
  const ms = (fix.lastTs ?? fix.ts) * 1000;
  const how = isCoarseFix(fix.src, fix.accM) ? `approximate, ${sourceLabel(fix.src)}` : sourceLabel(fix.src);
  return { text: `Position ${ago(ms)} (${how})`, stale: isStaleFix(ms, nowMs) };
}
