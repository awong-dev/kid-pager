/** TypeScript mirrors of the Firestore documents the web app reads, limited to rendered fields. */

import type { Timestamp } from "firebase/firestore";

// `admin` is a family admin; `super` is the global admin.
export type Role = "super" | "admin" | "member";

export type UserKind = "person" | "external";

// The policy code sets (`policy.ts`) are validated server-side, so these are plain strings.
export interface PolicyDoc {
  out: string;
  in: string;
}

export interface UserDoc {
  alias: string;
  displayName: string;
  email: string | null;
  phone: string | null;
  role: Role;
  disabled: boolean;
  createdAt: Timestamp | null;
  // Null for externals (an SMS number has no family).
  familyId: string | null;
  kind: UserKind;
  policy: PolicyDoc;
  notify: { alerts: boolean };
  // Set by the user's bridge phone (SIM or Google Voice number, E.164). Read-only.
  smsNumber?: string | null;
}

/** `families/{fid}.settings`. */
export interface FamilyDoc {
  name: string;
  blockedNumbers: string[];
  createdAt: Timestamp | null;
  createdBy: string;
}

// A family admin's inbox of things needing a decision. `chat_unknown`: a bridged Google Chat
// conversation nobody subscribed. `hook_down`: a hook failed three calls running (super only).
export type AlertStatus = "open" | "handled" | "dismissed";

export interface AlertDoc {
  kind: AlertKind;
  status: AlertStatus;
  ts: Timestamp | null;
  subjectUid: string;
  subjectAlias: string;
  peerUid: string | null;
  peerAlias: string | null;
  peerName: string | null;
  peerPhone: string | null;
  preview: string;
  heldBody?: string | null;
  // Texts held for an unknown number.
  heldCount?: number;
  updatedAt?: Timestamp | null;
  // `chat_unknown` only: `convRef` is what the Subscribe/Ignore routes take.
  bridgeId?: string;
  conversationId?: string;
  convRef?: string;
  convTitle?: string;
  isGroup?: boolean;
  people?: string[];
  source?: BridgeSource;
  convKey: string | null;
  contactRequestKey: string | null;
  decidedAt: Timestamp | null;
  decidedBy: string | null;
}

// Which app on the bridge phone an event or a send belongs to.
export type BridgeSource = "sms" | "gchat" | "gvoice" | "whatsapp";

export type BackendKind = "pager" | "webapp" | "gchat" | "sms" | "bridge";

export interface BackendDoc {
  kind: BackendKind;
  config: Record<string, unknown>;
  enabled: boolean;
  verifiedAt: Timestamp | null;
}

export interface AllowEdgeDoc {
  fromUid: string;
  toUid: string;
  message: boolean;
  locate: boolean;
  // Sorted unique non-null familyId of each end; length 2 marks a cross-family edge.
  familyIds?: string[];
}

export type TlsState = "unpinned" | "pinned" | "broken" | "proxy";

export interface DeviceStatusDoc {
  state: string | null;
  mode: string | null;
  battMv: number | null;
  tempDc?: number | null;
  rssi: number | null;
  session: string | null;
  ts: number | null;
  fw: string | null;
  locPeriodS: number | null;
  locMinS: number | null;
  // CA trust state and the pinned CA's short fingerprint.
  tls?: TlsState | null;
  caFp?: string | null;
  // Seconds until the next GPS attempt is allowed, 0 = now.
  locBackoffS?: number | null;
  // 1 = GNSS enabled, 0 = disabled by cfg.
  gnss?: number | null;
  // Running image short id (16 hex), the OTA gate (1 = firmware + rollback bootloader
  // confirmed), and the job's progress.
  img?: string | null;
  otaCap?: number | null;
  otaTarget?: string | null;
  otaState?: "wait" | "dl" | "ready" | "inst" | "ok" | "fail" | "rb" | null;
  otaPct?: number | null;
  otaErr?: string | null;
  // `/status` keys 72/73: 0 = font OTA supported but no assets_ota row, 1 = factory fonts
  // active, 2 = the OTA partition is active (`assetsId` = its 16-hex id).
  assetsSrc?: number | null;
  assetsId?: string | null;
  // Audit-queue SMS entries dropped on the pager before upload.
  smsLost?: number | null;
  // Carrier label (e.g. "Soracom").
  car?: string | null;
  // Serving cell of the latest `/loc` report that carried one, whether or not its position
  // resolved; only `ts` is mirrored.
  lastCell?: { ts: number } | null;
  updatedAt: Timestamp | null;
}

export interface DeviceDoc {
  ownerUid: string;
  label: string;
  mqttUsername: string;
  defaultToUid: string | null;
  revokedAt: Timestamp | null;
  locatableBy: string[];
  status: DeviceStatusDoc;
  // Set from the owner's `familyId` at device creation.
  familyId?: string | null;
  // The pending OTA job stored on push; null after cancel.
  otaJob?: OtaJob | null;
}

export interface OtaJob {
  target16: string;
  kind: "full" | "delta";
  osz: number;
  estBytes: number;
  onDemand?: boolean;
  /** "assets" for a font job; absent = app. */
  part?: "assets" | null;
  by_uid: string;
  at: number;
}

export interface LocationFixDoc {
  ts: number;
  fixTs: number;
  lat: number;
  lon: number;
  accM: number | null;
  src: string;
  cached: boolean;
  reqId: string | null;
  createdAt: Timestamp | null;
  // `why` is the reason the report was sent; `cellKey` the serving cell; `lastTs` is set when a
  // later report extended this dwell doc.
  why?: string | null;
  cellKey?: string | null;
  lastTs?: number | null;
}

export type MessageKind = "text" | "loc_req" | "loc";
export type DeliveryState =
  | "queued"
  | "sent"
  | "shown"
  | "read"
  | "fulfilled"
  | "failed"
  | "expired";

export interface DeliveryDoc {
  kind: string;
  state: DeliveryState;
  attempts: number;
  externalId: string | null;
  error: string | null;
  sentTs: number | null;
  shownTs: number | null;
  readTs: number | null;
}

export interface MessageLocDoc {
  lat: number;
  lon: number;
  accM?: number | null;
  fixTs: number;
  src?: string;
}

export interface MessageDoc {
  seq: number;
  convKey: string;
  uids: [string, string];
  senderUid: string;
  recipientUid: string;
  kind: MessageKind;
  body: string | null;
  loc: MessageLocDoc | null;
  wireId: string | null;
  originBackendKind: string | null;
  originBackendId: string | null;
  ts: number;
  createdAt: Timestamp | null;
  deliveries: Record<string, DeliveryDoc>;
  pendingDeviceIds: string[];
  // One id shared by every copy of a group message (the client dedupes bubbles on it) and the
  // author's alias; both null on a DM or pager-originated message.
  groupMsgId: string | null;
  senderAlias: string | null;
  // Every family with a uid in this message; lets the family-admin monitor thread query
  // `familyIds array-contains fam` without being in `uids`.
  familyIds?: string[];
}

export interface ConversationDoc {
  // A DM has exactly two `uids`, and no `kind`/`name`/`alias`/`createdBy`; absent `kind` is `"dm"`.
  uids: string[];
  lastMessageAt: Timestamp | null;
  lastPreview: string;
  unread: Record<string, number>;
  kind?: "dm" | "group";
  name?: string;
  alias?: string;
  createdBy?: string;
  // Set on a group bridged to a Google Chat space; its member routes return 409, so no leave.
  bridge?: { bridgeId: string; conversationId: string; source: string; link?: string | null } | null;
  // Per-member alias/name snapshot keyed by uid, to name a peer without a directory lookup.
  participants?: Record<string, { alias: string; displayName: string; kind: UserKind; phone?: string | null }>;
  // Every family with a member in this conversation, for the family-admin read-only view.
  familyIds?: string[];
}


}

}

// A loc_req never `fulfilled` expires 15 minutes after creation (derived; docs/PROTOCOL.md §13.4).
export const LOC_REQ_TTL_MS = 15 * 60 * 1000;

// Bridge phones are server-only, so these are API response shapes; times are ISO-8601 strings.
export type ApiTime = string | null;

export interface BridgeStatus {
  battery?: number | null;
  listenerBound?: boolean;
  smsDefault?: boolean;
  accessibility?: boolean;
  accounts?: string[];
  simNumber?: string | null;
  voiceNumber?: string | null;
  tier2Count?: number;
  version?: string | null;
  error?: string | null;
  unpaired?: boolean;
}

export interface BridgeRow {
  id: string;
  ownerUid: string;
  ownerAlias: string | null;
  ownerName: string | null;
  label: string;
  paired: boolean;
  pairedAt: ApiTime;
  lastSeenAt: ApiTime;
  // Accepted numbers; `status.*Number` is as reported.
  simNumber: string | null;
  voiceNumber: string | null;
  status: BridgeStatus;
  caps: { sms: boolean; gchat: boolean; gvoice: boolean; whatsapp: boolean };
  createdAt: ApiTime;
}

export type BridgeConversationStatus = "seen" | "subscribed" | "ignored" | "paused";

export interface RosterEntry {
  name: string;
  nick: string;
}

export interface BridgeConversationRow {
  // sha256(conversationId)[:16] -- what every conversation route takes.
  ref: string;
  bridgeId: string;
  conversationId: string;
  source: "gchat" | "gvoice" | "whatsapp";
  title: string | null;
  isGroup: boolean;
  link: string | null;
  people: string[];
  lastPreview: string;
  lastAt: ApiTime;
  firstSeenAt: ApiTime;
  inspectedAt: ApiTime;
  heldCount: number;
  status: BridgeConversationStatus;
  uid: string | null;
  convKey: string | null;
  pagerName: string | null;
  customName: boolean;
  alertId: string | null;
  // Subscribed / paused rows only.
  alias?: string | null;
  canReply?: boolean | null;
  roster?: RosterEntry[] | null;
  onPager?: boolean | null;
}

export interface ChatTabOut {
  subscribed: BridgeConversationRow[];
  seen: BridgeConversationRow[];
  // The member's bridge phones (for Add by link).
  bridges: { id: string; label: string; paired: boolean; caps: { sms: boolean; gchat: boolean; gvoice: boolean; whatsapp: boolean } }[];
}

export interface SubscribeRequest {
  pagerName: string;
  canReply: boolean;
  roster: RosterEntry[];
}

export interface SubscribeResult {
  uid: string;
  convKey: string | null;
  alias: string;
  delivered: number;
  undelivered: number;
  conversation: BridgeConversationRow;
}
