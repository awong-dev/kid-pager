/** Federation API client: family routes under `/api/family/fed` (404 unless federation is
 * effective), super routes under `/api/admin/fed`. */

import { ApiError, api } from "./api";
import { familyQuery } from "./family-context";

const seg = encodeURIComponent;

/** `health.state` is `ok` or `down`; `since` is epoch seconds. */
export interface FedHealth {
  state: "ok" | "down" | string;
  since: number;
}

export interface FedRemote {
  fedId: string;
  name: string;
  host: string;
  port: number;
  /** URL prefix the relay is mounted under: "" at the root, else e.g. "/kid-pager". */
  path: string;
  fingerprint: string;
}

/** `LinkOut` */
export interface FedLink {
  id: string;
  remote: FedRemote;
  state: "active" | "suspended" | "reauth" | string;
  /** A reauthorization was accepted on the invite code alone: compare keys. */
  remoteKeyChanged: boolean;
  /** Uids of OUR family's members the remote family may see. */
  shared: string[];
  health: FedHealth;
  /** Messages queued for the remote family. */
  pending: number;
  /** A family move is being announced to the remote family. */
  announcePending: boolean;
  /** ISO 8601 datetime, or null. */
  createdAt: string | null;
  /** ISO 8601 datetime, or null. */
  lastOkAt: string | null;
}

/** `GET /api/admin/fed/links` rows. */
export type FedAdminLink = FedLink & { familyId: string; familyName: string };

export interface FedThisFamily {
  fedId: string;
  name: string;
  fingerprint: string;
  links: FedLink[];
}

export interface FedDirectoryItem {
  rid: string;
  alias: string;
  name: string;
  /** This family already has a contact for the person. */
  added: boolean;
}

const base = () => "/family/fed";
const linkPath = (id: string) => `${base()}/links/${seg(id)}`;

export const getFamilyFed = () => api.get<FedThisFamily>(`${base()}${familyQuery()}`);
/** `expiresAt` is epoch seconds. */
export const newFedInvite = () => api.post<{ invite: string; expiresAt: number }>(`${base()}/invites${familyQuery()}`);
export const acceptFedInvite = (invite: string, confirmReauth = false) =>
  api.post<FedLink>(`${base()}/links${familyQuery()}`, confirmReauth ? { invite, confirmReauth: true } : { invite });
export interface FedReauthInvite {
  invite: string;
  /** Link into the other family's web app, or null when their base is unknown. */
  url: string | null;
  /** Epoch seconds. */
  expiresAt: number;
}
export const reauthFedLink = (id: string) => api.post<FedReauthInvite>(`${linkPath(id)}/reauth${familyQuery()}`);
export const suspendFedLink = (id: string) => api.post<FedLink>(`${linkPath(id)}/suspend${familyQuery()}`);
export const resumeFedLink = (id: string) => api.post<FedLink>(`${linkPath(id)}/resume${familyQuery()}`);
export const deleteFedLink = (id: string) => api.del<unknown>(`${linkPath(id)}${familyQuery()}`);
export const putFedShared = (id: string, shared: string[]) =>
  api.put<FedLink>(`${linkPath(id)}/shared${familyQuery()}`, { shared });
export const getFedDirectory = (id: string) => api.get<FedDirectoryItem[]>(`${linkPath(id)}/directory${familyQuery()}`);
export const addFedContact = (
  id: string,
  body: { rid: string; members: string[]; alias?: string; name?: string }
) =>
  api.post<{ uid: string; alias: string; displayName: string; linkId: string; rid: string; members: string[] }>(`${linkPath(id)}/contacts${familyQuery()}`, body);

/** `GET/PUT /api/admin/fed/state` (super). */
export interface FedState {
  enabled: boolean;
  enabledAt: string | null;
  families: { id: string; name: string; enabled: boolean }[];
}

export const getFedState = () => api.get<FedState>("/admin/fed/state");
export const setFedEnabled = (enabled: boolean) => api.put<FedState>("/admin/fed/state", { enabled });
export const setFedFamily = (fid: string, enabled: boolean) =>
  api.put<FedState>(`/admin/fed/families/${seg(fid)}`, { enabled });

export const listAdminFedLinks = () => api.get<FedAdminLink[]>("/admin/fed/links");
export const suspendAdminFedLink = (id: string) => api.post<FedAdminLink>(`/admin/fed/links/${seg(id)}/suspend`);
export const resumeAdminFedLink = (id: string) => api.post<FedAdminLink>(`/admin/fed/links/${seg(id)}/resume`);
export const deleteAdminFedLink = (id: string) => api.del<unknown>(`/admin/fed/links/${seg(id)}`);

/** `host[:port]path` for a linked server; the port is shown only when it is not 443. */
export function formatFedServer(host: string, port?: number | null, path?: string | null): string {
  return `${host}${port && port !== 443 ? `:${port}` : ""}${path ?? ""}`;
}

/** A fingerprint for a list: the first 19 characters (e.g. four groups of
 * four) and an ellipsis when it is longer. */
export function shortFingerprint(fp: string, max = 19): string {
  return fp.length > max ? `${fp.slice(0, max)}…` : fp;
}

const FED_502: Record<string, string> = {
  unreachable: "That family's server could not be reached.",
  tls: "That server's security certificate could not be verified.",
  refused: "That server refused the link. The invite may be used up or expired.",
  address_not_allowed: "That server's address is not allowed from here.",
  bad_response: "That server answered something this one does not understand.",
};

/** Exact `detail` strings of the relay's 409 answers on the link routes. */
const FED_409: Record<string, string> = {
  "already linked": "Your family is already linked to that family.",
  "link limit reached": "This family already has 16 links, the most allowed.",
  "that invite is from this family": "That invite is from your own family.",
  "reauthorize this link first": "This link was restored from a backup. Re-authorize it first.",
  "this link does not need reauthorization": "This link does not need re-authorizing.",
  "link is suspended": "This link is suspended. Resume it first.",
};

/** Plain sentence for a failed link or invite call. */
export function fedErrorText(e: unknown, fallback = "Request failed"): string {
  if (!(e instanceof ApiError)) return fallback;
  const d = typeof e.detail === "string" ? e.detail : "";
  if (e.status === 400) return "That invite is not valid. Ask the other admin for a new one.";
  if (e.status === 409) return FED_409[d] ?? "That link cannot be made right now.";
  if (e.status === 502) return FED_502[d] ?? "Could not reach the other family's server.";
  return d || e.message || fallback;
}

/** `active`, `suspended`, `reauth`, or `offline` (active but the remote is down). */
export function linkStateLabel(l: Pick<FedLink, "state" | "health">): "active" | "suspended" | "reauth" | "offline" {
  if (l.state === "suspended") return "suspended";
  if (l.state === "reauth") return "reauth";
  return l.health?.state === "down" ? "offline" : "active";
}

/** The move export answers 400 when the passphrase header is missing. */
export function isPassphraseRequired(e: unknown): boolean {
  return e instanceof ApiError && e.status === 400 && /passphrase/i.test(String(e.detail ?? e.message));
}

/** The move export answers 409 for a family that is not frozen. */
export function isNotFrozen(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409;
}

/** `fed` on a book entry or contact; `linkId`, `name` and `host` are null
 * once the link is gone. `rid` is on contacts only. */
export interface FedRef {
  linkId: string | null;
  name: string | null;
  host: string | null;
  rid?: string | null;
}

export function fedRemoteName(f: FedRef): string {
  return f.name ?? "another family";
}

export function fedRemoteHost(f: FedRef): string | null {
  return f.host;
}

/** Name and host of the remote family on a `fed` delivery (`server` is the
 * family's name, `serverHost` its host). */
export function fedDeliveryLabel(d: { server?: string | null; serverHost?: string | null }): {
  name: string | undefined;
  host: string | undefined;
} {
  return { name: d.server ?? undefined, host: d.serverHost ?? undefined };
}

/** The `fed_moved` alert: `fedHost` is the NEW host. */
export function fedMovedInfo(a: { fedLinkId?: string; fedName?: string; fedHost?: string; fedOldHost?: string }): {
  text: string;
  linkId: string | null;
} {
  return {
    text: `${a.fedName ?? "A linked family"} moved from ${a.fedOldHost ?? "its old server"} to ${a.fedHost ?? "a new server"}. Suspend the link if you did not expect this.`,
    linkId: a.fedLinkId ?? null,
  };
}

/** The invite in a `#fedInvite=<invite>` URL fragment, or null. */
export function parseFedInviteHash(hash: string): string | null {
  const h = hash.startsWith("#") ? hash.slice(1) : hash;
  for (const part of h.split("&")) {
    const i = part.indexOf("=");
    if (i < 0 || part.slice(0, i) !== "fedInvite") continue;
    let v = part.slice(i + 1);
    try {
      v = decodeURIComponent(v);
    } catch {
      /* keep the raw value */
    }
    v = v.trim();
    return v || null;
  }
  return null;
}

/** The link id in a `#federation&link=<id>` URL fragment, or null. */
export function parseFedLinkHash(hash: string): string | null {
  const h = hash.startsWith("#") ? hash.slice(1) : hash;
  for (const part of h.split("&")) {
    if (!part.startsWith("link=")) continue;
    try {
      return decodeURIComponent(part.slice(5)) || null;
    } catch {
      return part.slice(5) || null;
    }
  }
  return null;
}

/** The 409 body that asks the admin to confirm a reauthorize invite. */
export interface ReauthConfirm {
  link: FedLink;
  newFingerprint: string;
  newHost: string;
  newPort: number;
  newPath: string;
}

/** `{"detail": {"code": "reauth_confirm", ...}}` -> the confirmation data, else null. */
export function reauthConfirmDetail(e: unknown): ReauthConfirm | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const d = e.detail;
  if (!d || typeof d !== "object" || (d as { code?: unknown }).code !== "reauth_confirm") return null;
  if (!(d as { link?: unknown }).link) return null;
  return d as ReauthConfirm;
}

/** The `fed_reauth` alert: the other family was restored from a backup and re-linked. */
export function fedReauthInfo(a: { fedLinkId?: string; fedName?: string; fedHost?: string }): {
  text: string;
  linkId: string | null;
} {
  return {
    text: `${a.fedName ?? "A linked family"} was restored from a backup and re-linked from ${a.fedHost ?? "a new server"}.`,
    linkId: a.fedLinkId ?? null,
  };
}

/** The import report's `federation` part. */
export interface ReportFederation {
  links: number;
  reauth: number;
  announcing: number;
}

/** Lines for the import preview / result from the typed counts. */
export function reportFederationLines(v: ReportFederation | undefined): { reauth: string | null; announcing: boolean } {
  if (!v) return { reauth: null, announcing: false };
  return {
    reauth:
      v.reauth > 0
        ? `${v.reauth} ${v.reauth === 1 ? "link" : "links"} to other families need re-authorizing: Family > People & contacts > Other families > Re-authorize`
        : null,
    announcing: v.announcing > 0,
  };
}

/** Element id of a link's row on the People page (the alerts' "Review link" target). */
export const fedLinkElementId = (linkId: string) => `fedlink-${linkId}`;
