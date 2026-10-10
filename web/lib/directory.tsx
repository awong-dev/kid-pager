"use client";

/** Family-scoped directory: a live `users` listener merged with a fetched `GET /api/directory`. */

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { api } from "./api";
import { useAuth } from "./auth-context";
import { useFamily } from "./family-context";
import type { ConversationDoc, Role, UserDoc, UserKind } from "./types";

// `GET /api/directory` entry shape, also used (minus `phone`) for `users` listener rows.
export interface DirectoryEntry {
  uid: string;
  alias: string;
  displayName: string;
  kind: UserKind;
  familyId: string | null;
  role: Role;
  phone?: string;
}

// A narrower shape than `DirectoryEntry` for the "New group" dialog's contact list.
export interface Contact {
  uid: string;
  alias: string;
  displayName: string;
}

// A group conversation this member belongs to, keyed by the group's `alias`.
export interface GroupInfo {
  convKey: string;
  alias: string;
  name: string;
  uids: string[];
  /** True for a group bridged to Google Chat: managed under Family -> Google Chat, no leave. */
  bridged: boolean;
}

interface DirectoryContextValue {
  /** `undefined` if `uid` has not been resolved by either source below. */
  byUid: (uid: string) => DirectoryEntry | undefined;
  /** Same lookup, keyed by `alias`. */
  byAlias: (alias: string) => DirectoryEntry | undefined;
  /** @deprecated alias for `byAlias(alias)?.uid`. */
  aliasToUid: (alias: string) => string | undefined;
  /** @deprecated alias for `byUid(uid)?.alias`. */
  uidToAlias: (uid: string) => string | undefined;
  /** @deprecated no-op. */
  learn: (uid: string, alias: string) => void;
  /** True once the family listener and the `/api/directory` fetch have both resolved. */
  isComplete: boolean;
  /** Every resolved person (family members plus allow-edge/conversation
   * peers, in or out of family), including self. */
  contacts: Contact[];
  /** Every resolved external (SMS) contact. */
  externals: DirectoryEntry[];
  groupByAlias: (alias: string) => GroupInfo | undefined;
}

const DirectoryContext = createContext<DirectoryContextValue | null>(null);

interface ApiDirectoryResponse {
  entries: DirectoryEntry[];
}

export function DirectoryProvider({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const { familyId } = useFamily();
  const [apiEntries, setApiEntries] = useState<Map<string, DirectoryEntry>>(new Map());
  const [apiLoaded, setApiLoaded] = useState(false);

  // Live: every member of the family in scope. With no family in scope `useLive` keeps the
      });

  // One-shot fetch of cross-family peers and externals, on sign-in and on every family switch.
  useEffect(() => {
    if (!me) {
      // Signed out: keep whatever the map held.
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.get<ApiDirectoryResponse>("/directory");
        if (cancelled) return;
        const next = new Map<string, DirectoryEntry>();
        for (const entry of resp.entries) {
          next.set(entry.uid, entry);
        }
        setApiEntries(next);
      } catch {
        // Transient failure: the family listener still resolves same-family peers.
      } finally {
        if (!cancelled) setApiLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [me, familyId]);

  // Group conversations this member belongs to; a DM doc has no `alias` and is skipped.
    }

  // Merge: the live listener wins over the API fetch for any uid both name; the fetch is the
  // only source for cross-family peers and externals. Self always resolves from `me`.
  const byUidMap = useMemo(() => {
    const merged = new Map<string, DirectoryEntry>(apiEntries);
    for (const [uid, entry] of familyEntries) {
      merged.set(uid, entry);
    }
    if (me) {
      const existing = merged.get(me.uid);
      merged.set(me.uid, {
        uid: me.uid,
        alias: me.alias,
        displayName: me.displayName,
        kind: me.kind,
        familyId: me.familyId,
        role: me.role,
        phone: existing?.phone,
      });
    }
    return merged;
  }, [apiEntries, familyEntries, me]);

  const byAliasMap = useMemo(() => {
    const inverse = new Map<string, DirectoryEntry>();
    for (const entry of byUidMap.values()) {
      inverse.set(entry.alias, entry);
    }
    return inverse;
  }, [byUidMap]);

  // No-op; declared without parameters so nothing is unused.
  const learn = useCallback(() => {}, []);

  const contacts = useMemo<Contact[]>(
    () =>
      Array.from(byUidMap.values())
        .filter((e) => e.kind === "person")
        .map((e) => ({ uid: e.uid, alias: e.alias, displayName: e.displayName })),
    [byUidMap]
  );

  const externals = useMemo(
    () => Array.from(byUidMap.values()).filter((e) => e.kind === "external"),
    [byUidMap]
  );

  const value: DirectoryContextValue = {
    byUid: (uid) => byUidMap.get(uid),
    byAlias: (alias) => byAliasMap.get(alias),
    aliasToUid: (alias) => byAliasMap.get(alias)?.uid,
    uidToAlias: (uid) => byUidMap.get(uid)?.alias,
    learn,
    isComplete: familyLoaded && apiLoaded,
    contacts,
    externals,
    groupByAlias: (alias) => groupsByAlias.get(alias),
  };

  return <DirectoryContext.Provider value={value}>{children}</DirectoryContext.Provider>;
}

export function useDirectory(): DirectoryContextValue {
  const ctx = useContext(DirectoryContext);
  if (!ctx) {
    throw new Error("useDirectory must be used within DirectoryProvider");
  }
  return ctx;
}
