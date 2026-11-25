/** Path prefixes answered by the relay, never by this app. No React: `next.config.ts` imports it
 * (dev rewrites to the relay), the router refuses to render them, and a test checks that
 * `firebase.json`'s relay rewrites list exactly these. */
export const API_PATH_PREFIXES = ["/api", "/webhooks"] as const;

/** True for `/api`, `/api/...`, `/webhooks`, `/webhooks/...` (not `/apiary`). */
export function isApiPath(pathname: string): boolean {
  return API_PATH_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}
