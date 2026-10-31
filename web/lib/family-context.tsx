"use client";

/**
 * Family scope: fixed to `me.familyId` for admin/member; a super picks from `families/{fid}`
 * (`?family=`, then `sessionStorage['pager.family']`, then `me.familyId`).
 */

import {
  type ReactNode,
  createContext,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

import { useAuth } from "./auth-context";
import { useLive } from "./live";
import { allFamiliesQuery } from "./live/queries";
import type { FamilyDoc } from "./types";

const STORAGE_KEY = "pager.family";

export interface FamilySummary {
  id: string;
  name: string;
}

interface FamilyContextValue {
  familyId: string | null;
  family: FamilySummary | null;
  setFamilyId: (id: string) => void;
  families: FamilySummary[];
  /** True for `super` -- whether a switcher should be shown at all. */
  canSwitch: boolean;
}

const FamilyContext = createContext<FamilyContextValue | null>(null);

// Mirrors the provider's resolved scope for `familyQuery()`, which runs outside React.
let currentScope: { familyId: string | null; canSwitch: boolean } = {
  familyId: null,
  canSwitch: false,
};

/** `?family=<fid>` for a super with a family selected, else `''`; append to `/api/family/*`. */
export function familyQuery(): string {
  if (!currentScope.canSwitch || !currentScope.familyId) return "";
  return `?family=${encodeURIComponent(currentScope.familyId)}`;
}

function readFamilyParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("family");
}

function readStoredFamily(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function FamilyProvider({ children }: { children: ReactNode }) {
  const { me, isSuper } = useAuth();
  // Lazy initializer reads `?family=` / sessionStorage once at mount.
  const [selected, setSelected] = useState<string | null>(
    () => readFamilyParam() ?? readStoredFamily()
  );

  // Super only: live `families` for the switcher and family display names.
  const { docs: familyDocs } = useLive<FamilyDoc>(isSuper ? allFamiliesQuery() : null);
  const families = useMemo<FamilySummary[]>(
    () =>
      familyDocs
        .map((d) => ({ id: d.id, name: d.data.name }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [familyDocs]
  );

  const familyId = isSuper ? (selected ?? me?.familyId ?? null) : (me?.familyId ?? null);

  // Layout effect, not passive: children's mount effects (the first `/api/family/*` fetch)
  // run before this provider's passive effect and would miss `?family=`.
  useLayoutEffect(() => {
    currentScope = { familyId, canSwitch: isSuper };
  }, [familyId, isSuper]);

  function setFamilyId(id: string) {
    setSelected(id);
    if (typeof window !== "undefined") {
      try {
        window.sessionStorage.setItem(STORAGE_KEY, id);
      } catch {
        // Storage full or unavailable (private browsing): the choice stays in memory.
      }
    }
  }

  const family = useMemo(
    () => families.find((f) => f.id === familyId) ?? null,
    [families, familyId]
  );

  const value: FamilyContextValue = {
    familyId,
    family,
    setFamilyId,
    families,
    canSwitch: isSuper,
  };

  return <FamilyContext.Provider value={value}>{children}</FamilyContext.Provider>;
}

export function useFamily(): FamilyContextValue {
  const ctx = useContext(FamilyContext);
  if (!ctx) {
    throw new Error("useFamily must be used within FamilyProvider");
  }
  return ctx;
}
