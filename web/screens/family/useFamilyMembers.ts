"use client";

/** The family's members, live, sorted by alias. One subscription per page: `useLive` does not dedupe
 * identical queries, so the People & contacts page calls this once and hands the result to its
 * sections. */

import { useMemo } from "react";

import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { familyMembersQuery } from "@/lib/live/queries";
import type { UserDoc } from "@/lib/types";

export interface MemberRow extends UserDoc {
  uid: string;
}

export interface FamilyMembers {
  members: MemberRow[];
  loading: boolean;
  error: boolean;
}

export function sortMembers(rows: MemberRow[]): MemberRow[] {
  return [...rows].sort((a, b) => a.alias.localeCompare(b.alias));
}

export function useFamilyMembers(): FamilyMembers {
  const { familyId } = useFamily();
  // No family in scope yet: `useLive` keeps what it held.
  const { docs, loading, error } = useLive<UserDoc>(familyId ? familyMembersQuery(familyId) : null);
  const members = useMemo(() => sortMembers(docs.map((d) => ({ uid: d.id, ...d.data }))), [docs]);
  return { members, loading, error: error !== null };
}
