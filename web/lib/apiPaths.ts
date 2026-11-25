/** Path prefixes answered by the relay, never by this app. No React: `next.config.ts` imports it
 * (dev rewrites to the relay), the router refuses to render them, and a test checks that
 * the relay rewrites in both `firebase.json` files (repo root deploys; `web/` previews) list exactly these. */
export const API_PATH_PREFIXES = ["/api", "/webhooks", "/bridge"] as const;

/** True for a prefix or anything under it: `/api`, `/api/...` (not `/apiary`). */
export function isApiPath(pathname: string): boolean {
  return API_PATH_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}
