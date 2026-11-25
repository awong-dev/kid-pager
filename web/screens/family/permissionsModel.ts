/** Pure helpers for `/family/permissions`: the `?who=` selection, the plain-language summary of a
 * person's rules, and who may see whose location. No React, so `__tests__` can cover them. */

import { inboundLabel, outboundLabel } from "@/lib/policy";
import type { PolicyDoc } from "@/lib/types";

/** The part of `GET /api/family/members/{uid}/approved` the page reads. */
export interface ApprovedData {
  people: { alias: string; message: boolean; locate: boolean }[];
  contacts: { uid: string; message: boolean }[];
}

export const PERMISSIONS_PATH = "/family/permissions";

/** Link to one person's permissions (or to the page itself with no alias). */
export function permissionsHref(alias?: string): string {
  return alias ? `${PERMISSIONS_PATH}?who=${encodeURIComponent(alias)}` : PERMISSIONS_PATH;
}

/** The `?who=` alias in a query string (`"?who=kid"`), or null. */
export function whoOf(search: string): string | null {
  const v = new URLSearchParams(search).get("who");
  return v && v.trim() ? v.trim() : null;
}

/** The listed person the URL names; null when nobody is named or the alias is not a family person. */
export function selectedPerson<T extends { alias: string }>(people: readonly T[], search: string): T | null {
  const who = whoOf(search);
  return who === null ? null : (people.find((p) => p.alias === who) ?? null);
}

/** "Can message People. Can be messaged by Anyone." */
export function rulesLine(policy: PolicyDoc): string {
  return `Can message: ${outboundLabel(policy.out)}. Can be messaged by: ${inboundLabel(policy.in)}.`;
}

const count = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** "3 people, 1 contact approved"; under Open the contacts are all included. */
export function approvedLine(data: ApprovedData, policyOut: string): string {
  const people = data.people.filter((p) => p.message).length;
  const contacts = data.contacts.filter((c) => c.message).length;
  const parts: string[] = [];
  if (people > 0) parts.push(count(people, "person", "people"));
  if (contacts > 0) parts.push(count(contacts, "contact"));
  const base = parts.length > 0 ? `${parts.join(", ")} approved` : "No one approved yet";
  return policyOut === "open" ? `${base}; every contact is included (Open)` : base;
}

/** Aliases of the people (from every member's approved data, keyed by alias of the owner) who are
 * allowed to see where `alias` is: the ones with a Locate switch on for them. Sorted. */
export function locateViewers(byOwner: Readonly<Record<string, ApprovedData | undefined>>, alias: string): string[] {
  const out: string[] = [];
  for (const [owner, data] of Object.entries(byOwner)) {
    if (owner === alias || !data) continue;
    if (data.people.some((p) => p.alias === alias && p.message && p.locate)) out.push(owner);
  }
  return out.sort((a, b) => a.localeCompare(b));
}

/** What a newly added person starts with. There is no family-wide default: the relay picks by role
 * (`relay/app/store/users.py`, `_DEFAULT_MEMBER_POLICY` / `_DEFAULT_ADMIN_POLICY`). */
export const NEW_PERSON_DEFAULTS: Record<"member" | "admin", PolicyDoc> = {
  member: { out: "people", in: "people" },
  admin: { out: "open", in: "any" },
};

export function newPersonLine(): string {
  const m = NEW_PERSON_DEFAULTS.member;
  const a = NEW_PERSON_DEFAULTS.admin;
  return (
    `A new member starts with Who they can message: ${outboundLabel(m.out)} and Who can message them: ` +
    `${inboundLabel(m.in)} (their own family plus anyone you approve). A new admin starts with ` +
    `${outboundLabel(a.out)} and ${inboundLabel(a.in)}. There is no family-wide default to change; set it per person.`
  );
}

/** Family-wide tallies for the empty detail pane: "Who they can message: 2 People, 1 Open". */
export function tallyLines(policies: readonly PolicyDoc[]): string[] {
  if (policies.length === 0) return [];
  const tally = (codes: string[], label: (c: string) => string): string => {
    const n = new Map<string, number>();
    for (const c of codes) n.set(c, (n.get(c) ?? 0) + 1);
    return [...n.entries()]
      .sort((a, b) => b[1] - a[1] || label(a[0]).localeCompare(label(b[0])))
      .map(([c, k]) => `${k} ${label(c)}`)
      .join(", ");
  };
  return [
    `Who they can message: ${tally(policies.map((p) => p.out), outboundLabel)}`,
    `Who can message them: ${tally(policies.map((p) => p.in), inboundLabel)}`,
  ];
}
