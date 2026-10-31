/** Live reads through the build's provider. */

import { liveProvider } from "./provider";
import type { LiveDoc, LiveQuery, LiveUnsubscribe } from "./types";

export type { LiveDoc, LiveQuery, LiveUnsubscribe } from "./types";
export { useLive } from "./useLive";

/** Callback form for listeners that keep their own cross-snapshot state. */
export function subscribeLive<T>(
  q: LiveQuery,
  onNext: (docs: LiveDoc<T>[]) => void,
  onError: (err: Error) => void = (err) => console.warn("live query failed", err)
): LiveUnsubscribe {
  return liveProvider.subscribe(q, onNext as (docs: LiveDoc<unknown>[]) => void, onError);
}

/** One-shot read: resolves with the first snapshot, rejects on the first error. */
export function getLiveOnce<T>(q: LiveQuery): Promise<LiveDoc<T>[]> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let unsubscribe: LiveUnsubscribe | null = null;
    // The callback can fire inside `subscribe` before `unsubscribe` is assigned; defer.
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      queueMicrotask(() => unsubscribe?.());
      fn();
    };
    unsubscribe = liveProvider.subscribe(
      q,
      (docs) => finish(() => resolve(docs as LiveDoc<T>[])),
      (err) => finish(() => reject(err))
    );
  });
}
