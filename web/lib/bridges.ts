/** Bridge phone API client; every route is family-admin, so calls append `familyQuery()`. */

import { api } from "./api";
import { familyQuery } from "./family-context";
import type {
  ApiTime,
  BridgeRow,
  ChatTabOut,
  RosterEntry,
  SubscribeRequest,
  SubscribeResult,
} from "./types";

const seg = encodeURIComponent;

export function toMs(v: ApiTime | undefined): number | null {
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms;
}

export interface BridgeCode {
  code: string;
  expiresAt: ApiTime;
}

export interface CreatedBridge extends BridgeCode {
  bridge: BridgeRow;
}


export const createBridge = (ownerUid: string, label: string) =>
  api.post<CreatedBridge>(`/family/bridges${familyQuery()}`, { ownerUid, label });

export const newBridgeCode = (id: string) =>
  api.post<BridgeCode>(`/family/bridges/${seg(id)}/code${familyQuery()}`);




export const inspectLink = (bridgeId: string, link: string) =>
  api.post<{ outboxId: string }>(`/family/bridges/${seg(bridgeId)}/inspect${familyQuery()}`, { link });

export const getChatTab = (uid: string) => api.get<ChatTabOut>(`/family/members/${seg(uid)}/chat${familyQuery()}`);

function convPath(bridgeId: string, ref: string): string {
  return `/family/bridges/${seg(bridgeId)}/conversations/${seg(ref)}`;
}

export const subscribeChat = (bridgeId: string, ref: string, body: SubscribeRequest) =>
  api.post<SubscribeResult>(`${convPath(bridgeId, ref)}/subscribe${familyQuery()}`, body);

export const ignoreChat = (bridgeId: string, ref: string) =>

export interface ChatPatch {
  pagerName?: string;
  canReply?: boolean;
  paused?: boolean;
  roster?: RosterEntry[];
}

export const patchChat = (bridgeId: string, ref: string, body: ChatPatch) =>

export const unsubscribeChat = (bridgeId: string, ref: string) =>


/** The wire's alias shape for a roster nick (`sndr`, docs/PROTOCOL.md §3.1). */
export const NICK_RE = /^[a-z0-9][a-z0-9_-]{0,15}$/;

/** Chat link prefixes the relay accepts. */
export function isGoogleChatLink(v: string): boolean {
  return /^https:\/\/(chat\.google\.com|voice\.google\.com)\//.test(v) || /^https:\/\/mail\.google\.com\/chat\//.test(v);
}

function fallbackNick(name: string): string {
  // Non-Latin names slug to nothing; the relay uses "p" + sha256[:6]. This is
  // only an editable default, so a cheap FNV-1a hash is enough.
  let h = 0x811c9dc5;
  for (const ch of name) {
    h ^= ch.codePointAt(0) ?? 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `p${h.toString(16).padStart(8, "0").slice(0, 6)}`;
}

/** Mirror of the relay's `slug_nick`: NFKD, drop non-ASCII, lowercase, runs of
 * non `[a-z0-9]` become `-`, strip `-`, cut to 16; on collision cut to 13 and
 * append `-2`, `-3`, ... `taken` is not modified. */
export function slugNick(name: string, taken: ReadonlySet<string>): string {
  const ascii = name.normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  let base = ascii
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 16)
    .replace(/-+$/, "");
  if (!base) base = fallbackNick(name);
  if (!taken.has(base)) return base;
  const stem = base.slice(0, 13).replace(/-+$/, "");
  for (let n = 2; ; n++) {
    const cand = `${stem}-${n}`;
    if (!taken.has(cand)) return cand;
  }
}

/** Default roster for a list of speaker names, nicks unique. Existing
 * `known` entries (name -> nick) are kept as they are. */
export function defaultRoster(names: string[], known: Record<string, string> = {}): RosterEntry[] {
  const taken = new Set<string>(Object.values(known));
  return names.map((name) => {
    const nick = known[name] ?? slugNick(name, taken);
    taken.add(nick);
    return { name, nick };
  });
}

/** Outbound people-rules `sms` and `any_sms` let nobody (person) be messaged; Subscribe then
 * forces canReply=false. */
export function outboundBlocksPeople(policyOut: string): boolean {
  return policyOut === "sms" || policyOut === "any_sms";
}
