/**
 * `useLive(query)`: the query is keyed by its JSON, so a fresh object each render does not
 * resubscribe. The last snapshot is kept while a changed query is pending; `loading` is true
 * until its first snapshot.
 */

import { useEffect, useState } from "react";

import { liveProvider } from "./provider";
import type { LiveDoc, LiveQuery } from "./types";

export interface LiveResult<T> {
  docs: LiveDoc<T>[];
  loading: boolean;
  error: Error | null;
}

interface State {
  key: string | null;
  docs: LiveDoc<unknown>[];
  error: Error | null;
}

const INITIAL: State = { key: null, docs: [], error: null };

export function useLive<T>(q: LiveQuery | null): LiveResult<T> {
  const key = q === null ? null : JSON.stringify(q);
  const [state, setState] = useState<State>(INITIAL);

  useEffect(() => {
    if (key === null) return;
    return liveProvider.subscribe(
      JSON.parse(key) as LiveQuery,
      (docs) => setState({ key, docs, error: null }),
      (error) => setState((prev) => ({ key, docs: prev.docs, error }))
    );
  }, [key]);

  return {
    docs: state.docs as LiveDoc<T>[],
    loading: key !== null && state.key !== key,
    error: state.key === key ? state.error : null,
  };
}
