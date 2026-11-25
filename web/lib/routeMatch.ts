/** Path matching for the client router (`lib/router.tsx`, route table in `app/routes.tsx`).
 * Patterns are `/`-separated segments; `:name` captures one non-empty segment. No React here. */

export type Params = Record<string, string>;

function segments(path: string): string[] {
  return path.split("/").filter(Boolean);
}

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** Params if `pathname` matches `pattern` exactly (a trailing slash is ignored), else null. */
export function matchPattern(pattern: string, pathname: string): Params | null {
  const want = segments(pattern);
  const got = segments(pathname);
  if (want.length !== got.length) return null;
  const params: Params = {};
  for (let i = 0; i < want.length; i++) {
    const w = want[i]!;
    const g = got[i]!;
    if (w.startsWith(":")) params[w.slice(1)] = decode(g);
    else if (w !== g) return null;
  }
  return params;
}

/** Positive if `a` is more specific than `b`: at the first segment where one is a literal and the
 * other a `:param`, the literal wins. Only meaningful for two patterns that can both match. */
function specificity(a: string, b: string): number {
  const sa = segments(a);
  const sb = segments(b);
  for (let i = 0; i < Math.min(sa.length, sb.length); i++) {
    const pa = sa[i]!.startsWith(":");
    const pb = sb[i]!.startsWith(":");
    if (pa !== pb) return pa ? -1 : 1;
  }
  return 0;
}

/** The most specific route whose pattern matches (ties: first listed), or null. */
export function matchRoute<R extends { path: string }>(
  routes: readonly R[],
  pathname: string,
): { route: R; params: Params } | null {
  let best: { route: R; params: Params } | null = null;
  for (const route of routes) {
    const params = matchPattern(route.path, pathname);
    if (!params) continue;
    if (!best || specificity(route.path, best.route.path) > 0) best = { route, params };
  }
  return best;
}
