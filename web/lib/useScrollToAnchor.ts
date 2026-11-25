"use client";

/** Scroll to the element a `#fragment` names once the page has its content. The router (History
 * API) does not do this: a lazily loaded screen has no element yet when the browser would, so a
 * deep link such as `/family/people#groups` needs the screen to ask. Runs once, the first time
 * `ready` is true; `ids` limits which anchors this page answers to. */

import { useEffect, useRef } from "react";

import { anchorOf } from "@/lib/anchor";

export function useScrollToAnchor(ready: boolean, ids: readonly string[]): void {
  const done = useRef(false);
  useEffect(() => {
    if (!ready || done.current) return;
    done.current = true;
    const id = anchorOf(window.location.hash);
    if (!id || !ids.includes(id)) return;
    const t = setTimeout(() => document.getElementById(id)?.scrollIntoView({ block: "start" }), 0);
    return () => clearTimeout(t);
    // `ids` is a constant list per page
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);
}
