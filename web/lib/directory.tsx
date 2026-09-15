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

interface DirectoryContextValue {
  aliasToUid: (alias: string) => string | undefined;
  uidToAlias: (uid: string) => string | undefined;
  learn: (uid: string, alias: string) => void;
  isComplete: boolean;
}

const DirectoryContext = createContext<DirectoryContextValue | null>(null);

export function DirectoryProvider({ children }: { children: ReactNode }) {

      });


    }
    return inverse;

  const value: DirectoryContextValue = {
    learn,
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
