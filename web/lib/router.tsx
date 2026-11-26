"use client";

/** Minimal client router: History API + `useSyncExternalStore`, no dependency.
 *
 * Navigation uses `window.history.pushState/replaceState`, which Next's App Router patches and
 * documents as supported (its own `usePathname` follows them); next/navigation's `router.push`
 * is deliberately not used because it would try to fetch a route that no longer exists. The
 * route table is `app/routes.tsx`; `app/page.tsx` renders it. */

import {
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  createContext,
  forwardRef,
  useContext,
  useMemo,
  useSyncExternalStore,
} from "react";

import { isApiPath } from "@/lib/apiPaths";
import { isUnderBase, stripBase, withBase } from "@/lib/basePath";
import type { Params } from "@/lib/routeMatch";

const NAV_EVENT = "pager:navigate";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange);
  window.addEventListener(NAV_EVENT, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(NAV_EVENT, onChange);
  };
}

/** The logical pathname: the URL path with the runtime prefix (lib/basePath.ts) removed. */
const getClientSnapshot = (): string | null => stripBase(window.location.pathname);
/** `null` on the server and during hydration, so the prerendered HTML (for `/`) matches the first
 * client render whatever URL the shell was served for. */
const getServerSnapshot = (): string | null => null;

/** Current pathname, or `null` until the client has mounted. */
export function usePathname(): string | null {
  return useSyncExternalStore(subscribe, getClientSnapshot, getServerSnapshot);
}

const getSearchSnapshot = (): string => window.location.search;
const getServerSearch = (): string => "";

/** Current query string (with its leading `?`, or `""`). Unlike `usePathname` it re-renders on a
 * `?query`-only navigation, and on Back/Forward, so a screen can keep its selection in the URL. */
export function useSearch(): string {
  return useSyncExternalStore(subscribe, getSearchSnapshot, getServerSearch);
}

const ParamsContext = createContext<Params>({});

/** Provides the matched route's `:param` values (decoded) to its screen. */
export function ParamsProvider({ params, children }: { params: Params; children: ReactNode }) {
  return <ParamsContext.Provider value={params}>{children}</ParamsContext.Provider>;
}

export function useParams(): Params {
  return useContext(ParamsContext);
}

/** Resolves a logical `href` (`/chat?x=1#y`, no prefix) against the current page; the URL's
 * pathname is the real one (prefixed). `isOwn` is false for another origin, a path outside the
 * prefix, or an API path: those are left to the browser. */
function resolve(href: string): { url: URL; isOwn: boolean } {
  const url = new URL(withBase(href), window.location.href);
  const isOwn = url.origin === window.location.origin && isUnderBase(url.pathname) && !isApiPath(stripBase(url.pathname));
  return { url, isOwn };
}

function go(href: string, mode: "push" | "replace"): void {
  const { url, isOwn } = resolve(href);
  if (!isOwn) {
    window.location.assign(url.href);
    return;
  }
  const target = `${url.pathname}${url.search}${url.hash}`;
  const pathChanged = url.pathname !== window.location.pathname;
  if (mode === "push") window.history.pushState(null, "", target);
  else window.history.replaceState(null, "", target);
  window.dispatchEvent(new Event(NAV_EVENT));
  if (mode === "push" && pathChanged && !url.hash) window.scrollTo(0, 0);
}

export type Navigate = {
  push: (href: string) => void;
  replace: (href: string) => void;
  back: () => void;
};

const NAVIGATE: Navigate = {
  push: (href) => go(href, "push"),
  replace: (href) => go(href, "replace"),
  back: () => window.history.back(),
};

/** Stable `{ push, replace, back }`; `href` is a logical path and may carry a query string and #fragment. */
export function useNavigate(): Navigate {
  return useMemo(() => NAVIGATE, []);
}

/** `<a href>` (a logical path, without the prefix; rendered with it) that navigates in-app on a plain left click; modified clicks, `target`, downloads
 * and other origins keep the browser's default. Works with MUI's `component={Link}`. */
export const Link = forwardRef<HTMLAnchorElement, AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; replace?: boolean }>(
  function Link({ href, replace, onClick, target, ...rest }, ref) {
    const handleClick = (e: MouseEvent<HTMLAnchorElement>) => {
      onClick?.(e);
      if (
        e.defaultPrevented ||
        e.button !== 0 ||
        e.metaKey ||
        e.ctrlKey ||
        e.shiftKey ||
        e.altKey ||
        (target && target !== "_self") ||
        rest.download !== undefined
      ) {
        return;
      }
      if (!resolve(href).isOwn) return;
      e.preventDefault();
      go(href, replace ? "replace" : "push");
    };
    return <a ref={ref} href={withBase(href)} target={target} onClick={handleClick} {...rest} />;
  },
);
