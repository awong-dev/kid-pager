/** Return path across the sign-in redirect, kept in `sessionStorage` and validated on both
 * sides so it is always a same-origin relative path (open-redirect guard). */

const KEY = "pager.returnPath";
const DEFAULT_PATH = "/chat";

/** A same-origin relative path: starts with a single `/`, no `//` or `/\`
 * prefix (protocol-relative), no scheme, no control characters. */
export function isSafeReturnPath(p: unknown): p is string {
  if (typeof p !== "string" || p.length === 0 || p.length > 4096) return false;
  if (p[0] !== "/" || p[1] === "/" || p[1] === "\\") return false;
  if (/[\u0000-\u001f\u007f\\]/.test(p)) return false;
  return true;
}

/** True for the pages that must never be a return target. */
function isAuthPage(p: string): boolean {
  return /^\/(login|setup)(\/|\?|#|$)/.test(p);
}

/** Remember where the person was going; no-op for anything unsafe. */
export function stashReturnPath(path: string): void {
  if (!isSafeReturnPath(path) || isAuthPage(path)) return;
  try {
    sessionStorage.setItem(KEY, path);
  } catch {
    /* storage unavailable: fall back to /chat */
  }
}

/** The stashed path (cleared), or `/chat`. */
export function takeReturnPath(): string {
  try {
    const p = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
    if (isSafeReturnPath(p) && !isAuthPage(p)) return p;
  } catch {
    /* ignore */
  }
  return DEFAULT_PATH;
}
