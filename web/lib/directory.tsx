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

import { useAuth } from "./auth-context";

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
}

interface DirectoryContextValue {
  aliasToUid: (alias: string) => string | undefined;
  uidToAlias: (uid: string) => string | undefined;
  learn: (uid: string, alias: string) => void;
  isComplete: boolean;
  contacts: Contact[];
  groupByAlias: (alias: string) => GroupInfo | undefined;
}

const DirectoryContext = createContext<DirectoryContextValue | null>(null);

export function DirectoryProvider({ children }: { children: ReactNode }) {

      });

  // Group conversations this member belongs to; a DM doc has no `alias` and is skipped.
    }


    }
    return inverse;

  const value: DirectoryContextValue = {
    learn,
    contacts,
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
