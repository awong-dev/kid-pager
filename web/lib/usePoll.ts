"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Runs `load` now and every `ms` while the tab is visible. `reload()` runs it at once.
 * Errors are kept in `error` and the last good `data` stays. */
export function usePoll<T>(load: () => Promise<T>, ms: number, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const first = setTimeout(() => void reload(), 0);
    const tick = setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, ms);
    return () => {
      clearTimeout(first);
      clearInterval(tick);
    };
  }, [reload, ms, enabled]);

  return { data, error, reload };
}
