"use client";

/** Each person's approved list for the Permissions overview: one `GET /api/family/members/{uid}/
 * approved` per person (the same call the editor makes), keyed by alias. `refresh(alias)` re-reads
 * one person after their editor saved. A person whose call fails is simply absent. */

import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { familyQuery } from "@/lib/family-context";
import type { ApprovedData } from "./permissionsModel";

export interface Summaries {
  byAlias: Record<string, ApprovedData | undefined>;
  /** True until the first round of calls has finished. */
  loading: boolean;
  refresh: (alias: string) => void;
}

export function useApprovedSummaries(people: readonly { uid: string; alias: string }[]): Summaries {
  const [byAlias, setByAlias] = useState<Record<string, ApprovedData | undefined>>({});
  const [loaded, setLoaded] = useState<string>("");
  // Re-run only when the set of people changes, not on every live snapshot of their docs.
  const key = useMemo(() => people.map((p) => `${p.uid}:${p.alias}`).join(","), [people]);

  const fetchOne = useCallback(async (uid: string, alias: string, isCancelled: () => boolean) => {
    try {
      const data = await api.get<ApprovedData>(`/family/members/${uid}/approved${familyQuery()}`);
      if (!isCancelled() && data && Array.isArray(data.people) && Array.isArray(data.contacts)) {
        setByAlias((prev) => ({ ...prev, [alias]: data }));
      }
    } catch {
      // the row shows its rules without a count
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const list = key ? key.split(",").map((s) => s.split(":") as [string, string]) : [];
    void Promise.all(list.map(([uid, alias]) => fetchOne(uid, alias, () => cancelled))).then(() => {
      if (!cancelled) setLoaded(key);
    });
    return () => {
      cancelled = true;
    };
  }, [key, fetchOne]);

  const refresh = useCallback(
    (alias: string) => {
      const p = people.find((x) => x.alias === alias);
      if (p) void fetchOne(p.uid, alias, () => false);
    },
    [people, fetchOne],
  );

  return { byAlias, loading: loaded !== key, refresh };
}
