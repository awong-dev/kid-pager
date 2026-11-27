/** The URL path prefix this copy of the app is served under (`""` or e.g. `"/pager"`).
 *
 * The home relay substitutes it into the exported files after minification, so it is read from the
 * DOM (`<html data-pager-base>`, written by `app/layout.tsx`), never from a build-time value.
 * Routes, `Link` hrefs and `navigate.push()` take "logical" paths without the prefix (`/chat`);
 * `withBase` makes one a real URL path and `stripBase` goes back. */

let cached: string | null = null;

/** `""` or `/prefix` (leading slash, no trailing slash). `""` while prerendering. */
export function basePath(): string {
  if (typeof document === "undefined") return "";
  if (cached === null) {
    const raw = document.documentElement.getAttribute("data-pager-base") ?? "";
    const trimmed = raw.replace(/\/+$/, "");
    cached = trimmed === "" || trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  }
  return cached;
}

/** Test hook: forget the cached value. */
export function resetBasePathCache(): void {
  cached = null;
}

/** A logical absolute path (`/chat?x=1#y`) as a real URL path. Anything else (a full URL, a
 * protocol-relative `//host`, a relative path, `#frag`) is returned unchanged. */
export function withBase(path: string): string {
  if (path[0] !== "/" || path[1] === "/") return path;
  return basePath() + path;
}

/** True if `pathname` (a real URL path) is inside the prefix; always true with no prefix. */
export function isUnderBase(pathname: string): boolean {
  const b = basePath();
  return b === "" || pathname === b || pathname.startsWith(`${b}/`);
}

/** A real URL path as a logical one: `/pager/chat` to `/chat`, `/pager` to `/`. A path outside
 * the prefix is returned unchanged. */
export function stripBase(pathname: string): string {
  const b = basePath();
  if (b === "" || !isUnderBase(pathname)) return pathname;
  return pathname.slice(b.length) || "/";
}

/** The origin plus prefix with no trailing slash, e.g. `https://host/pager`: this server's URL
 * for a person to copy or a phone to be configured with. `""` while prerendering. */
export function serverUrl(): string {
  return typeof window === "undefined" ? "" : window.location.origin + basePath();
}
