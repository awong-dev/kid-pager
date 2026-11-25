/** The route table: who can reach which screen. `app/page.tsx` renders it; tests import it.
 * (Lives beside `page.tsx` because a Next page file may only export the page component.)
 *
 * Four blocks, in this order: PUBLIC, AUTHENTICATED (the default), REDIRECTS, API.
 * A path is public only if it is listed in PUBLIC. Anything not listed anywhere is
 * authenticated: the router guards it first, then shows Not Found inside the shell.
 * The navigation (top links, section tabs, bottom bar) is a separate table:
 * `components/shell/sections.ts`. */

import type { ComponentType } from "react";

import type { Role } from "@/components/RequireAuth";
import { API_PATH_PREFIXES, isApiPath } from "@/lib/apiPaths";
import { type Params, matchRoute } from "@/lib/routeMatch";

export type Screen = () => Promise<{ default: ComponentType }>;
export type PublicRoute = { path: string; screen: Screen };
export type AuthRoute = { path: string; role: Role; screen: Screen };

// ================================================================================================
// PUBLIC: no sign-in, no app shell. Only paths listed here are public.
// ================================================================================================
export const PUBLIC: readonly PublicRoute[] = [
  { path: "/login",                 screen: () => import("@/screens/login/Login") },
  { path: "/setup",                 screen: () => import("@/screens/setup/Setup") },
];

// ================================================================================================
// AUTHENTICATED: the default. Every row is wrapped in RequireAuth(role) + AppShell by the router.
//   role "member" = any signed-in user; "admin" = family admin or super; "super" = super only.
//   Unlisted paths are authenticated too (member), then Not Found.
// ================================================================================================
export const AUTHENTICATED: readonly AuthRoute[] = [
  // --- member: any signed-in user -----------------------------------------------------------
  { path: "/",                      role: "member", screen: () => import("@/screens/home/Home") },
  { path: "/chat",                  role: "member", screen: () => import("@/screens/chat/Messages") },
  { path: "/chat/:alias",           role: "member", screen: () => import("@/screens/chat/Thread") },
  { path: "/devices/:id",           role: "member", screen: () => import("@/screens/devices/Device") },
  { path: "/location",              role: "member", screen: () => import("@/screens/location/Location") },
  { path: "/account/apps",          role: "member", screen: () => import("@/screens/account/Backends") },
  { path: "/account/book",          role: "member", screen: () => import("@/screens/account/Book") },
  { path: "/account/notifications", role: "member", screen: () => import("@/screens/account/Notifications") },
  { path: "/account/pagers",        role: "member", screen: () => import("@/screens/account/Pagers") },

  // --- admin: family admin or super ---------------------------------------------------------
  { path: "/chat/view/:key",        role: "admin",  screen: () => import("@/screens/chat/MonitorThread") },
  { path: "/alerts",                role: "admin",  screen: () => import("@/screens/alerts/Alerts") },
  { path: "/family/chat",           role: "admin",  screen: () => import("@/screens/family/Chat") },
  { path: "/family/devices",        role: "admin",  screen: () => import("@/screens/family/Devices") },
  { path: "/family/people",         role: "admin",  screen: () => import("@/screens/family/People") },
  { path: "/family/permissions",    role: "admin",  screen: () => import("@/screens/family/Permissions") },
  { path: "/family/settings",       role: "admin",  screen: () => import("@/screens/family/Settings") },

  // --- super: superadmin only ---------------------------------------------------------------
  { path: "/admin/allowlist",       role: "super",  screen: () => import("@/screens/admin/Allowlist") },
  { path: "/admin/devices",         role: "super",  screen: () => import("@/screens/admin/Devices") },
  { path: "/admin/families",        role: "super",  screen: () => import("@/screens/admin/Families") },
  { path: "/admin/federation",      role: "super",  screen: () => import("@/screens/admin/Federation") },
  { path: "/admin/hooks",           role: "super",  screen: () => import("@/screens/admin/Hooks") },
  { path: "/admin/users",           role: "super",  screen: () => import("@/screens/admin/Users") },
];

// ================================================================================================
// REDIRECTS: old paths that moved, so bookmarks, notification links and emails keep working.
//   `from` is an exact path; the query string and #fragment ride along (page.tsx / router).
//   Checked before AUTHENTICATED; the target's own role guard still applies after the redirect.
//   Never link to a `from` path inside the app: use `to`.
// ================================================================================================
export type RedirectRoute = { from: string; to: string };
export const REDIRECTS: readonly RedirectRoute[] = [
  { from: "/family/alerts",         to: "/alerts" },
  { from: "/settings/backends",     to: "/account/apps" },
  { from: "/settings/book",         to: "/account/book" },
  { from: "/settings/devices",      to: "/account/pagers" },
  { from: "/settings/notifications", to: "/account/notifications" },
  { from: "/family/contacts",       to: "/family/people#contacts" },
  { from: "/family/history",        to: "/family/settings" },
  { from: "/admin/settings",        to: "/family/settings" },
];

// ================================================================================================
// API: answered by the relay, never by this app (Hosting and the dev server send them there).
// The router never renders these; if one is reached client-side it does a full page load.
// ================================================================================================
export const API: readonly string[] = API_PATH_PREFIXES; // /api, /webhooks, /bridge  (lib/apiPaths.ts)

export type Resolved =
  | { kind: "redirect"; to: string }
  | { kind: "api" }
  | { kind: "public"; route: PublicRoute; params: Params }
  | { kind: "authenticated"; role: Role; route: AuthRoute | null; params: Params };

/** Pure: which block a pathname belongs to. Not listed anywhere = authenticated, no screen. */
export function resolveRoute(pathname: string): Resolved {
  if (isApiPath(pathname)) return { kind: "api" };
  const pub = matchRoute(PUBLIC, pathname);
  if (pub) return { kind: "public", route: pub.route, params: pub.params };
  const moved = REDIRECTS.find((r) => r.from === pathname.replace(/(?<=.)\/+$/, ""));
  if (moved) return { kind: "redirect", to: moved.to };
  const auth = matchRoute(AUTHENTICATED, pathname);
  if (auth) return { kind: "authenticated", role: auth.route.role, route: auth.route, params: auth.params };
  return { kind: "authenticated", role: "member", route: null, params: {} };
}
