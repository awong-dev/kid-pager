/** Deployment move (docs/MOVE_DESIGN.md): wire types, API calls and the pure rules the Move screens use. */

import { api } from "./api";
import { withBase } from "./basePath";
import { looksLikeEmail } from "./emailList";

// ---- wire types ----------------------------------------------------------------------------------

export type MoveBackend = "home" | "firebase";
export type Bearer = "direct" | "beam";
/** Times arrive as ISO 8601 strings; epoch seconds or milliseconds are accepted too. */
export type TimeValue = string | number | null | undefined;

/** `GET /api/public/move-info` (§7.1). */
export interface MoveInfo {
  v: number;
  authBackend: MoveBackend;
  schemaVersion: number;
  relayVersion: string;
  baseUrl: string;
  broker: { host: string; port: number };
  ca: { url: string; sha: string } | null;
  beam: { host: string; port: number } | null;
  empty: boolean;
  move: { id: string; imported: boolean } | null;
}

/** `POST /api/admin/move/preflight` (§7.4): the target's move-info plus `caFingerprint`. */
export interface PreflightResult extends MoveInfo {
  caFingerprint?: string | null;
}

/** One person's answer in the accounts panel (§7.4, §9). */
export interface AccountAnswer {
  uid: string;
  email?: string;
  noSignIn?: boolean;
  password?: string;
}

export interface FreezeRequest {
  targetBaseUrl: string;
  /** PEM of the target's own certificate authority, when it is not a public one. */
  caPem?: string;
  confirm: { host: string; port: number; caSha: string | null };
  accounts: AccountAnswer[];
}

export type MoveState = "none" | "frozen" | "cutover" | "cancelled" | "ended" | "importing" | "imported";

/** Source device row (§7.2, §12.4 `devices/{d}.move` on the source, plus bearer and liveness). */
export interface SourceDeviceRow {
  deviceId: string;
  label: string;
  owner?: string | null;
  capable: boolean;
  sentAt?: TimeValue;
  ackAt?: TimeValue;
  last?: { st: PagerMoveSt; rsn?: PagerMoveReason | null; at?: number | null; ts?: TimeValue } | null;
  cancelledAt?: TimeValue;
  bearer?: Bearer | null;
  lastSeenAt?: TimeValue;
  darkSince?: TimeValue;
}

/** `/status mv` values (§12.2). */
export type PagerMoveSt = "pend" | "try" | "ok" | "fail" | "cxl";
export type PagerMoveReason =
  | "ca" | "tls" | "auth" | "conn" | "proof" | "budget" | "nvs" | "rej" | "down" | "busy";

export type TargetDeviceState = "waiting" | "trying" | "moved" | "reprovisioned";
export type Remedy = "none" | "switch_now_or_setup" | "setup_code";

/** Target device row (§7.4). */
export interface TargetDeviceRow {
  deviceId: string;
  label: string;
  owner?: string | null;
  state: TargetDeviceState;
  bearer?: Bearer | null;
  lastSeenAt?: TimeValue;
  darkSince?: TimeValue;
  remedy: Remedy;
}

/** A bridge phone. On the source `redirected` says whether it was told to move; on the target
 * `arrived` says it has made an authenticated request here since the import. */
export interface BridgeRow {
  bridgeId: string;
  label?: string | null;
  owner?: string | null;
  lastSeenAt?: TimeValue;
  redirected?: boolean;
  arrived?: boolean;
}

/** A person. Source: how they will sign in on the target. Target: whether they have. */
export interface PersonRow {
  uid: string;
  alias: string;
  displayName?: string | null;
  email?: string | null;
  noSignIn?: boolean;
  signedIn?: boolean;
  lastSignInAt?: TimeValue;
}

export interface FedLinkRow {
  linkId: string;
  family?: string | null;
  peer: string;
  announced: boolean;
}

/** Target Beam block; `null` when the target has no Beam listener (`BEAM_PORT` unset). */
export interface BeamInfo {
  host?: string;
  port?: number;
  /** Pagers whose last `/status` said `tls: "proxy"`. */
  count: number;
  /** How many of them are `moved`. */
  movedCount: number;
  switched: boolean;
  /** The target has the Soracom key, so the switch can be done here. */
  soracom: boolean;
  /** The switch was confirmed by hand, so switching back is by hand too. */
  byHand?: boolean;
}

export interface SourceMove {
  role: "source";
  id: string;
  state: MoveState;
  target: { baseUrl: string; authBackend?: MoveBackend; host?: string; port?: number; caSha?: string | null; privateCa?: boolean };
  /** The target's `move-info` shows this move imported. */
  targetImported?: boolean;
  devices: SourceDeviceRow[];
  bridges: BridgeRow[];
  people: PersonRow[];
}

export interface TargetMove {
  role: "target";
  id: string;
  state: MoveState;
  sourceBaseUrl: string;
  importedAt?: TimeValue;
  devices: TargetDeviceRow[];
  bridges: BridgeRow[];
  people: PersonRow[];
  fedLinks: FedLinkRow[];
  beam: BeamInfo | null;
  /** Bridges are being sent back to the source. */
  bridgeRedirect?: string | null;
}

export type AdminMove = { role: "idle" } | SourceMove | TargetMove;

/** `GET /api/me/move` (§7.4). */
export interface MyMove {
  targetBaseUrl: string;
  claim?: { url: string } | null;
  email?: string | null;
}

/** Import report (dry run, real, and the body of a refused import); only the fields the screen reads. */
export interface ImportReport {
  ok?: boolean;
  errors?: string[];
  notes?: string[];
  counts?: Record<string, number>;
  emailsTaken?: string[];
  emailsMissing?: string[];
  emailsInvalid?: string[];
  /** Things in the bundle that already exist on this server. */
  collisions?: { kind: string; id: string }[];
  /** Env-level secrets the source had set (§5.3). */
  envSecrets?: string[];
}

// ---- calls ----------------------------------------------------------------------------------------

/** The relay's own move-info (no sign-in). `null` on any failure. */
export async function getOwnMoveInfo(): Promise<MoveInfo | null> {
  try {
    const resp = await fetch(withBase("/api/public/move-info"), { cache: "no-store" });
    return resp.ok ? ((await resp.json()) as MoveInfo) : null;
  } catch {
    return null;
  }
}

export const preflight = (targetBaseUrl: string, caPem?: string) =>
  api.post<PreflightResult>("/admin/move/preflight", caPem ? { targetBaseUrl, caPem } : { targetBaseUrl });

export const freeze = (body: FreezeRequest) => api.post<{ moveId: string }>("/admin/move/freeze", body);

export const downloadBundle = (passphrase: string) =>
  api.getBlob("/admin/move/bundle", { "X-Export-Passphrase": passphrase });

export const cutover = (deviceIds?: string[]) =>
  api.post<{ ordered: number }>("/admin/move/cutover", deviceIds ? { deviceIds } : {});

export const resendOrder = (deviceId: string) =>
  api.post(`/admin/move/devices/${encodeURIComponent(deviceId)}/resend`);

export const cancelDevice = (deviceId: string) =>
  api.post(`/admin/move/devices/${encodeURIComponent(deviceId)}/cancel`);

export const cancelMove = () => api.post("/admin/move/cancel");

export const importBundle = (file: Blob, passphrase: string, dryRun: boolean) =>
  api.postFile<ImportReport>(`/admin/move/import${dryRun ? "?dryRun=1" : ""}`, file, {
    "X-Export-Passphrase": passphrase,
  });

export const beamSwitch = (byHand: boolean) => api.post("/admin/move/beam/switch", { byHand });
export const beamSwitchBack = () => api.post("/admin/move/beam/switch-back");
export const bridgesSendBack = () => api.post("/admin/move/bridges/send-back");
export const endMove = () => api.post("/admin/move/end");

/** `GET /api/admin/move`, normalised. A relay with no move answers `{"role":"idle","state":"none"}`;
 * a 404 is read the same way. */
export async function getAdminMove(): Promise<AdminMove> {
  try {
    return normalizeAdminMove(await api.get<unknown>("/admin/move"));
  } catch (e) {
    if (typeof e === "object" && e !== null && (e as { status?: number }).status === 404) return { role: "idle" };
    throw e;
  }
}

/** `GET /api/me/move`; `null` when there is no move (404) or nothing to show. */
export async function getMyMove(): Promise<MyMove | null> {
  try {
    const m = await api.get<Partial<MyMove> | null>("/me/move");
    return m && typeof m.targetBaseUrl === "string" && m.targetBaseUrl !== "" ? (m as MyMove) : null;
  } catch {
    return null;
  }
}

// ---- normalising `GET /admin/move` ----------------------------------------------------------------

type Raw = Record<string, unknown>;
const arr = <T>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);

/** Which side this relay is on is read from the answer: the target's carries `sourceBaseUrl`, the
 * source's `target`; an explicit `role` wins. No `id`, or state `none`, is idle. */
export function normalizeAdminMove(raw: unknown): AdminMove {
  if (!raw || typeof raw !== "object") return { role: "idle" };
  const r = raw as Raw;
  const state = (typeof r.state === "string" ? r.state : "none") as MoveState;
  if (typeof r.id !== "string" || r.id === "" || state === "none") return { role: "idle" };
  const isTarget = r.role === "target" || (r.role !== "source" && typeof r.sourceBaseUrl === "string");
  const common = { id: r.id, state, bridges: arr<BridgeRow>(r.bridges), people: arr<PersonRow>(r.people) };
  if (isTarget) {
    return {
      role: "target",
      ...common,
      sourceBaseUrl: typeof r.sourceBaseUrl === "string" ? r.sourceBaseUrl : "",
      importedAt: r.importedAt as TimeValue,
      devices: arr<TargetDeviceRow>(r.devices),
      fedLinks: arr<FedLinkRow>(r.fedLinks),
      beam: (r.beam as BeamInfo | null | undefined) ?? null,
      bridgeRedirect: (r.bridgeRedirect as string | null | undefined) ?? null,
    };
  }
  const t = (r.target && typeof r.target === "object" ? r.target : {}) as SourceMove["target"];
  return {
    role: "source",
    ...common,
    target: { ...t, baseUrl: typeof t.baseUrl === "string" ? t.baseUrl : "" },
    targetImported: typeof r.targetImported === "boolean" ? r.targetImported : undefined,
    devices: arr<SourceDeviceRow>(r.devices),
  };
}

// ---- small pure helpers ---------------------------------------------------------------------------

/** Epoch ms of a wire time, or `null`. */
export function toMillis(v: TimeValue): number | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return v < 1e11 ? v * 1000 : v;
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms;
}

/** "2h 10m", "35m", "4d 3h"; under a minute is "under 1m". */
export function formatDuration(ms: number): string {
  const m = Math.floor(Math.max(0, ms) / 60_000);
  if (m < 1) return "under 1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

/** The base URL to send to preflight: https only (the relay refuses anything else), no query,
 * fragment or trailing slash; a bare host gets `https://`. */
export function normalizeTargetUrl(input: string): { url: string } | { error: string } {
  const text = input.trim();
  if (!text) return { error: "Enter the new server's address." };
  let url: URL;
  try {
    url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`);
  } catch {
    return { error: "That is not a web address." };
  }
  if (url.protocol !== "https:") {
    return { error: "The new server's address must start with https://." };
  }
  if (url.username || url.password) return { error: "Leave the user name and password out of the address." };
  return { url: `${url.origin}${url.pathname.replace(/\/+$/, "")}` };
}

/** A SHA-256 hex string as 8-character groups for reading aloud. */
export function groupHex(hex: string): string {
  return (hex.match(/.{1,8}/g) ?? []).join(" ");
}

export function caFingerprintOf(p: Pick<PreflightResult, "ca" | "caFingerprint">): string | null {
  return p.caFingerprint || p.ca?.sha || null;
}

/** The `confirm` block Freeze sends: exactly what the person was shown. */
export function confirmOf(p: PreflightResult): FreezeRequest["confirm"] {
  return { host: p.broker.host, port: p.broker.port, caSha: caFingerprintOf(p) };
}

/** Warnings to show next to the target's details before Freeze. */
export function preflightWarnings(p: PreflightResult): string[] {
  const out: string[] = [];
  if (!p.empty) out.push("The new server already holds families. It will refuse the import until it is empty.");
  if (p.move) out.push("The new server already has a move on record.");
  if (!p.ca) out.push("The new server reports no broker certificate authority. Pinned pagers cannot move to it.");
  return out;
}

const PEM_CERT = /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/;

/** The PEM to send as `caPem`: the certificate block(s) of what was pasted or picked, or `null`. */
export function caPemOf(text: string): string | null {
  const t = text.trim();
  return PEM_CERT.test(t) ? t : null;
}

/** Whether a preflight/Freeze refusal is the source failing to verify the target's certificate. */
export function isTlsFailure(status: number, detail: unknown): boolean {
  return status === 502 && typeof detail === "string" && /did not answer over TLS: /.test(detail);
}

export const PASSPHRASE_MIN = 8;
/** HTTP header values must be Latin-1; the passphrase travels in one. */
const PRINTABLE_ASCII = /^[\x20-\x7e]+$/;

/** Why a bundle passphrase is not acceptable, or `null`. */
export function passphraseProblem(pass: string, again?: string): string | null {
  if (pass.length > 0 && !PRINTABLE_ASCII.test(pass)) return "Use plain ASCII characters only.";
  if (pass.length < PASSPHRASE_MIN) return `At least ${PASSPHRASE_MIN} characters.`;
  if (again !== undefined && pass !== again) return "The two passphrases differ.";
  return null;
}

// ---- accounts panel -------------------------------------------------------------------------------

export const PASSWORD_MIN = 8;
/** The longest password an admin can set for a person in the accounts panel. */
export const PASSWORD_MAX = 128;
/** The longest password the sign-in routes take (a person's own, at first sign-in or later). */
export const LOGIN_PASSWORD_MAX = 256;

export interface AccountPerson {
  uid: string;
  alias: string;
  displayName: string;
  /** The email already stored for the person, if any. */
  email: string | null;
}

/** What the admin has entered for one person. */
export interface AccountDraft {
  email: string;
  noSignIn: boolean;
  /** Home target: also set a password for the person. */
  setPassword: boolean;
  password: string;
}

export const emptyDraft = (p: AccountPerson): AccountDraft => ({
  email: p.email ?? "",
  noSignIn: false,
  setPassword: false,
  password: "",
});

export interface AccountsCheck {
  /** Per person: what is wrong, if anything. */
  errors: Record<string, string>;
  ok: boolean;
  /** The `accounts` array for Freeze (one entry per person). */
  answers: AccountAnswer[];
}

/** What each target backend needs (§9). Home: nothing, a password of 8 to 128 characters is
 * optional. Firebase: an email or an explicit "No sign-in" for every person, no email twice. */
export function checkAccounts(
  backend: MoveBackend,
  people: readonly AccountPerson[],
  drafts: Readonly<Record<string, AccountDraft>>,
): AccountsCheck {
  const errors: Record<string, string> = {};
  const answers: AccountAnswer[] = [];
  const seen = new Map<string, string>();
  for (const p of people) {
    const d = drafts[p.uid] ?? emptyDraft(p);
    if (backend === "home") {
      if (d.setPassword && (d.password.length < PASSWORD_MIN || d.password.length > PASSWORD_MAX)) {
        errors[p.uid] = `The password needs ${PASSWORD_MIN} to ${PASSWORD_MAX} characters.`;
      }
      answers.push(d.setPassword ? { uid: p.uid, password: d.password } : { uid: p.uid });
      continue;
    }
    if (d.noSignIn) {
      answers.push({ uid: p.uid, noSignIn: true });
      continue;
    }
    const email = d.email.trim();
    const key = email.toLowerCase();
    if (!email) errors[p.uid] = "Enter an email, or choose No sign-in.";
    else if (!looksLikeEmail(email)) errors[p.uid] = "That is not an email address.";
    else if (seen.has(key)) errors[p.uid] = "Another person already has this email.";
    else seen.set(key, p.uid);
    answers.push({ uid: p.uid, email });
  }
  return { errors, ok: Object.keys(errors).length === 0, answers };
}

/** What a Freeze `422` named: people without an answer, people whose answer was refused (uid to
 * message), and uids this server does not know. */
export interface FreezeProblems {
  missing: string[];
  invalid: Record<string, string>;
  unknown: string[];
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** Reads the extras beside `detail` in an error body; empty for any other body. */
export function freezeProblems(body: unknown): FreezeProblems {
  const out: FreezeProblems = { missing: [], invalid: {}, unknown: [] };
  if (!body || typeof body !== "object") return out;
  const b = body as { missing?: unknown; invalid?: unknown; unknown?: unknown };
  out.missing = strings(b.missing);
  out.unknown = strings(b.unknown);
  if (b.invalid && typeof b.invalid === "object") {
    for (const [uid, msg] of Object.entries(b.invalid)) if (typeof msg === "string") out.invalid[uid] = msg;
  }
  return out;
}

/** The target's details as they now are, from a Freeze `409 {detail, broker, caSha}`; `null` if the body has none. */
export function changedDetails(body: unknown): { host: string; port: number; caSha: string | null } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { broker?: { host?: unknown; port?: unknown }; caSha?: unknown };
  if (!b.broker || typeof b.broker.host !== "string" || typeof b.broker.port !== "number") return null;
  return { host: b.broker.host, port: b.broker.port, caSha: typeof b.caSha === "string" ? b.caSha : null };
}

/** The import report inside a refused import's `409` body (the whole report sits beside `detail`). */
export function reportOfRefusal(body: unknown): ImportReport | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  return Array.isArray(b.errors) || Array.isArray(b.collisions) || b.counts ? (b as ImportReport) : null;
}

// ---- source pager stages --------------------------------------------------------------------------

export type SourceStage =
  | "incapable" | "ready" | "ordered" | "acked" | "trying" | "moved" | "failed" | "cancelled";

/** Where a pager is in the cutover, from what the source knows. Later facts win: a report from
 * the pager beats the order's ack, which beats the order being sent. */
export function sourceStage(r: SourceDeviceRow): SourceStage {
  if (r.cancelledAt || r.last?.st === "cxl") return "cancelled";
  if (!r.capable) return "incapable";
  if (r.last?.st === "ok") return "moved";
  if (r.last?.st === "fail") return "failed";
  if (r.last?.st === "try") return "trying";
  if (r.ackAt) return "acked";
  if (r.sentAt) return "ordered";
  return "ready";
}

export const REASON_TEXT: Record<PagerMoveReason, string> = {
  ca: "could not fetch the new server's certificate",
  tls: "the secure connection to the new server failed",
  auth: "the new server refused its login",
  conn: "could not reach the new server",
  proof: "the new server did not prove it is the right one",
  budget: "used up its tries",
  nvs: "could not save the new settings",
  rej: "refused the order",
  down: "refused the order (a pinned pager needs the certificate in it)",
  busy: "was busy",
};

export const reasonText = (rsn: string | null | undefined): string | null =>
  rsn ? (REASON_TEXT[rsn as PagerMoveReason] ?? rsn) : null;

export type ChipColor = "default" | "primary" | "success" | "warning" | "error" | "info";
export interface ChipSpec {
  label: string;
  color: ChipColor;
  /** Pending states are outlined, finished ones filled (as the delivery chips are). */
  filled: boolean;
  /** Mark kept beside the label so the state does not rest on colour alone. */
  mark: "done" | "wait" | "work" | "problem" | "stop";
}

/** `capable: false` covers old firmware, a pager that does not sign its messages (password
 * login) and one that does not report; the row does not say which, so the wording fits all three. */
export const SOURCE_CHIP: Record<SourceStage, ChipSpec> = {
  incapable: { label: "Cannot move by itself", color: "warning", filled: false, mark: "problem" },
  ready: { label: "Ready", color: "default", filled: false, mark: "wait" },
  ordered: { label: "Order sent", color: "info", filled: false, mark: "wait" },
  acked: { label: "Has the order", color: "info", filled: true, mark: "wait" },
  trying: { label: "Connecting to new server", color: "info", filled: true, mark: "work" },
  moved: { label: "Moved", color: "success", filled: true, mark: "done" },
  failed: { label: "Failed", color: "error", filled: true, mark: "problem" },
  cancelled: { label: "Cancelled", color: "default", filled: true, mark: "stop" },
};

/** The one line of detail beside a source row's chip, or `null`. */
export function sourceDetail(r: SourceDeviceRow): string | null {
  const stage = sourceStage(r);
  const tries = r.last?.at ? ` after ${r.last.at} ${r.last.at === 1 ? "try" : "tries"}` : "";
  if (stage === "failed") {
    const why = reasonText(r.last?.rsn);
    return why ? `Last try: ${why}${tries}.` : `The last try failed${tries}.`;
  }
  if (stage === "acked" && r.last?.st === "pend" && r.last.rsn) {
    return `Has the order; a try failed (${reasonText(r.last.rsn)}). It will try again.`;
  }
  if (stage === "acked") return "Has the order; waiting for it to connect to the new server.";
  if (stage === "ordered") return "The order is sent; the pager has not confirmed it (it may be off).";
  if (stage === "incapable") return "This pager cannot be told to move. It needs a setup code from the new server.";
  if (stage === "cancelled") return "Its order was cancelled, and it ignores this move from now on. It needs a setup code from the new server.";
  if (stage === "trying") return `Trying the new server${r.last?.at ? ` (try ${r.last.at})` : ""}.`;
  return null;
}

/** Ordering for a status list: what needs a person first, finished last. */
const ATTENTION: Record<SourceStage, number> = {
  failed: 0, incapable: 1, ordered: 2, acked: 3, trying: 4, ready: 5, cancelled: 6, moved: 7,
};

export function sortSource(rows: readonly SourceDeviceRow[]): SourceDeviceRow[] {
  return [...rows].sort(
    (a, b) => ATTENTION[sourceStage(a)] - ATTENTION[sourceStage(b)] || a.label.localeCompare(b.label),
  );
}

export interface Tally {
  total: number;
  /** `[count, label]` in display order, zero counts left out. */
  parts: [number, string][];
  /** Pagers a person has to act on. */
  needsPerson: number;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "12 pagers: 7 moved, 2 trying, ..." from a tally. */
export function tallyText(t: Tally, noun = "pager"): string {
  if (t.total === 0) return `No ${noun}s.`;
  const parts = t.parts.map(([n, l]) => `${n} ${l}`).join(", ");
  return `${plural(t.total, noun)}${parts ? `: ${parts}` : ""}`;
}

export function sourceTally(rows: readonly SourceDeviceRow[]): Tally {
  const c: Record<SourceStage, number> = {
    incapable: 0, ready: 0, ordered: 0, acked: 0, trying: 0, moved: 0, failed: 0, cancelled: 0,
  };
  for (const r of rows) c[sourceStage(r)]++;
  const order: [SourceStage, string][] = [
    ["moved", "moved"], ["trying", "connecting"], ["acked", "have the order"], ["ordered", "order sent"],
    ["ready", "ready"], ["failed", "failed"], ["incapable", "cannot move by itself"], ["cancelled", "cancelled"],
  ];
  return {
    total: rows.length,
    parts: order.filter(([s]) => c[s] > 0).map(([s, l]) => [c[s], l]),
    needsPerson: c.failed + c.incapable,
  };
}

/** Capable pagers the cutover will order. */
export const capableCount = (rows: readonly SourceDeviceRow[]) => rows.filter((r) => r.capable).length;

// ---- target pager states and remedies -------------------------------------------------------------

export const TARGET_CHIP: Record<TargetDeviceState, ChipSpec> = {
  waiting: { label: "Not arrived yet", color: "warning", filled: false, mark: "wait" },
  trying: { label: "Arriving", color: "info", filled: true, mark: "work" },
  moved: { label: "Arrived", color: "success", filled: true, mark: "done" },
  reprovisioned: { label: "Set up again", color: "success", filled: true, mark: "done" },
};

export const isArrived = (r: Pick<TargetDeviceRow, "state">) => r.state === "moved" || r.state === "reprovisioned";

/** Whether a not-arrived pager has been silent past its keepalive window (the relay computes it). */
export const isDark = (r: Pick<TargetDeviceRow, "state" | "darkSince">) => !isArrived(r) && toMillis(r.darkSince) !== null;

export interface RemedyText {
  /** What to do, in a sentence. */
  text: string;
  /** Show the "Issue a setup code" button. */
  setupCode: boolean;
}

/** The manual remedy of §4.2 for a pager not arrived. While the source answers, the admin can
 * look at its page to tell the two cases apart; after, the pager's own screen tells them. */
export function remedyFor(
  row: Pick<TargetDeviceRow, "state" | "remedy">,
  ctx: { host: string; sourceUp: boolean },
): RemedyText | null {
  if (isArrived(row) || row.remedy === "none") return null;
  if (row.remedy === "setup_code") {
    return {
      text: "This pager cannot move by itself. Issue a setup code here, then on the pager choose Set up again and type it.",
      setupCode: true,
    };
  }
  const now = `On the pager's Device screen choose "Switch to ${ctx.host} now"`;
  const setup = "issue a setup code here and choose Set up again on the pager";
  return {
    text: ctx.sourceUp
      ? `Check the old server's status page. If it shows this pager as having the order: ${now.charAt(0).toLowerCase()}${now.slice(1)}. If not: ${setup}.`
      : `${now} if it shows a move. If it shows nothing: ${setup}.`,
    setupCode: true,
  };
}

/** Row order on the target: needs a person, then in progress, then arrived. */
function targetRank(r: TargetDeviceRow): number {
  if (isArrived(r)) return 4;
  if (r.remedy !== "none" && isDark(r)) return 0;
  if (r.remedy !== "none") return 1;
  return r.state === "trying" ? 3 : 2;
}

export function sortTarget(rows: readonly TargetDeviceRow[]): TargetDeviceRow[] {
  return [...rows].sort((a, b) => targetRank(a) - targetRank(b) || a.label.localeCompare(b.label));
}

export function targetTally(rows: readonly TargetDeviceRow[]): Tally & { arrived: number; dark: number } {
  const n = (s: TargetDeviceState) => rows.filter((r) => r.state === s).length;
  const dark = rows.filter(isDark).length;
  const parts: [number, string][] = [
    [n("moved"), "arrived"], [n("reprovisioned"), "set up again"], [n("trying"), "arriving"],
    [n("waiting"), "not yet"], [dark, "dark"],
  ];
  return {
    total: rows.length,
    arrived: n("moved") + n("reprovisioned"),
    dark,
    parts: parts.filter(([c]) => c > 0),
    needsPerson: rows.filter((r) => !isArrived(r) && r.remedy !== "none").length,
  };
}

export const BEARER_LABEL: Record<Bearer, string> = { direct: "Direct", beam: "Beam" };

/** `tls: "proxy"` is how a pager on Beam reports itself; the relay's `bearer` is already direct or beam. */
export const bearerLabel = (b: string | null | undefined): string | null =>
  b === "beam" || b === "proxy" ? BEARER_LABEL.beam : b === "direct" ? BEARER_LABEL.direct : null;

// ---- bridges, people, Beam ------------------------------------------------------------------------

/** Heartbeats come every 5 minutes; a bridge not heard for twice that is no longer on this server. */
export const BRIDGE_HERE_WINDOW_MS = 10 * 60_000;

/** On the target: a bridge that is still reaching this server. */
export function bridgeStillHere(b: BridgeRow, now: number): boolean {
  const seen = toMillis(b.lastSeenAt);
  return b.arrived === true && seen !== null && now - seen < BRIDGE_HERE_WINDOW_MS;
}

export interface BridgeTally { here: number; notYet: number; gone: number; total: number }

/** Bridges on the target: reaching it now, never arrived, or arrived and gone quiet. */
export function bridgeTally(rows: readonly BridgeRow[], now: number): BridgeTally {
  const here = rows.filter((b) => bridgeStillHere(b, now)).length;
  const notYet = rows.filter((b) => !b.arrived).length;
  return { here, notYet, gone: rows.length - here - notYet, total: rows.length };
}

export interface PeopleTally { signedIn: number; notYet: number; noSignIn: number }

export function peopleTally(rows: readonly PersonRow[]): PeopleTally {
  const noSignIn = rows.filter((p) => p.noSignIn).length;
  const signedIn = rows.filter((p) => !p.noSignIn && p.signedIn).length;
  return { signedIn, notYet: rows.length - noSignIn - signedIn, noSignIn };
}

/** Switch back moves the recorded SIMs and is refused once a Beam pager has moved (§10). */
export function beamActions(b: BeamInfo): { canSwitch: boolean; canSwitchBack: boolean; switchBackByHand: boolean } {
  return {
    canSwitch: !b.switched && b.count > 0,
    canSwitchBack: b.switched && b.movedCount === 0 && !b.byHand,
    switchBackByHand: b.switched && b.byHand === true && b.movedCount === 0,
  };
}

/** Why Switch back is unavailable, for the line under the disabled button; `null` when it is not blocked by arrivals. */
export function beamSwitchBackBlock(b: BeamInfo): string | null {
  return b.switched && b.movedCount > 0
    ? `${plural(b.movedCount, "Beam pager")} already moved here, so the group cannot be switched back.`
    : null;
}

/** "Send bridges back" has nothing to do until bridges exist, and is idle once sent. */
export function sendBackState(bridgeRedirect: string | null | undefined, rows: readonly BridgeRow[]): "send" | "sent" | "none" {
  if (bridgeRedirect) return "sent";
  return rows.length === 0 ? "none" : "send";
}

// ---- the banner on the source ---------------------------------------------------------------------

export type BannerKind = "claim" | "email" | "plain";

/** What a signed-in person sees on the frozen source: their claim link, the email to sign in
 * with, or just where to go. */
export function bannerKind(m: MyMove): BannerKind {
  if (m.claim?.url) return "claim";
  if (m.email) return "email";
  return "plain";
}

/** The login page on the target (for the plain and email banners). */
export const targetLoginUrl = (targetBaseUrl: string) => `${targetBaseUrl.replace(/\/+$/, "")}/login`;

/** Where the target's Move page is, for the link from the source. */
export const moveAdminUrl = (baseUrl: string) => `${baseUrl.replace(/\/+$/, "")}/admin/move`;

export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

// ---- the sign-in flows on the home backend --------------------------------------------------------

/** The invite code in `#invite=<code>` (the claim link, §9), or `null`. */
export function parseInviteFragment(hash: string): string | null {
  const m = /^#?(?:.*&)?invite=([^&]*)/.exec(hash);
  if (!m) return null;
  let code: string;
  try {
    code = decodeURIComponent(m[1]!).trim();
  } catch {
    return null;
  }
  return code !== "" && code.length <= 64 && !/\s/.test(code) ? code : null;
}

/** Why a first-sign-in password change cannot be sent, or `null`. */
export function passwordChangeProblem(current: string, next: string, again: string): string | null {
  if (!current) return "Enter the password your admin gave you.";
  if (next.length < PASSWORD_MIN) return `The new password needs at least ${PASSWORD_MIN} characters.`;
  if (next.length > LOGIN_PASSWORD_MAX) return `The new password can have at most ${LOGIN_PASSWORD_MAX} characters.`;
  if (next === current) return "Choose a password different from the one your admin set.";
  if (next !== again) return "The two new passwords differ.";
  return null;
}

// ---- what is down, and for whom (§1) --------------------------------------------------------------

export interface DowntimeRow {
  who: string;
  /** What they cannot do, and what is kept for them. */
  text: string;
}

/** Plain statement of §1 for the admin: down from Freeze, until each group arrives. */
export const DOWNTIME: readonly DowntimeRow[] = [
  {
    who: "People",
    text: "Cannot send from the web or app from Freeze until they sign in on the new server (minutes after the import). They can still read here, under a banner that says where to go.",
  },
  {
    who: "Pagers",
    text: "A text sent from a pager is refused here and kept by the pager, which sends it to the new server after it moves. Nothing is lost. Pages written on the new server wait for a pager that has not arrived.",
  },
  {
    who: "Bridge phones",
    text: "Their messages are refused here, so each phone keeps them queued and sends them to the new server within about 5 minutes of the import.",
  },
  {
    who: "Linked families on other servers",
    text: "Messages to and from them wait (they are told to retry) until they accept the new server's address, automatically, after the import.",
  },
];
