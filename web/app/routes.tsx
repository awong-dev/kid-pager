/** The route table: who can reach which screen. `app/page.tsx` renders it; tests import it.
 * (Lives beside `page.tsx` because a Next page file may only export the page component.)
 *
 * Three blocks, in this order: PUBLIC, AUTHENTICATED (the default), API.
 * A path is public only if it is listed in PUBLIC. Anything not listed anywhere is
 * authenticated: the router guards it first, then shows Not Found inside the shell. */

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
//   Unlisted paths are authenticated too (member), then Not Found. "/" redirects to /chat.
// ================================================================================================
export const AUTHENTICATED: readonly AuthRoute[] = [
  // --- member: any signed-in user -----------------------------------------------------------
  { path: "/chat",                  role: "member", screen: () => import("@/screens/chat/List") },
  { path: "/chat/:alias",           role: "member", screen: () => import("@/screens/chat/Thread") },
  { path: "/devices/:id",           role: "member", screen: () => import("@/screens/devices/Device") },
  { path: "/location",              role: "member", screen: () => import("@/screens/location/Location") },
  { path: "/settings/backends",     role: "member", screen: () => import("@/screens/settings/Backends") },
  { path: "/settings/book",         role: "member", screen: () => import("@/screens/settings/Book") },
  { path: "/settings/devices",      role: "member", screen: () => import("@/screens/settings/Devices") },
  { path: "/settings/notifications", role: "member", screen: () => import("@/screens/settings/Notifications") },

  // --- admin: family admin or super ---------------------------------------------------------
  { path: "/chat/view/:key",        role: "admin",  screen: () => import("@/screens/chat/MonitorThread") },
  { path: "/family/alerts",         role: "admin",  screen: () => import("@/screens/family/Alerts") },
  { path: "/family/chat",           role: "admin",  screen: () => import("@/screens/family/Chat") },
  { path: "/family/contacts",       role: "admin",  screen: () => import("@/screens/family/Contacts") },
  { path: "/family/devices",        role: "admin",  screen: () => import("@/screens/family/Devices") },
  { path: "/family/people",         role: "admin",  screen: () => import("@/screens/family/People") },
  { path: "/admin/settings",        role: "admin",  screen: () => import("@/screens/admin/Settings") },

  // --- super: superadmin only ---------------------------------------------------------------
  { path: "/admin/allowlist",       role: "super",  screen: () => import("@/screens/admin/Allowlist") },
  { path: "/admin/devices",         role: "super",  screen: () => import("@/screens/admin/Devices") },
  { path: "/admin/families",        role: "super",  screen: () => import("@/screens/admin/Families") },
  { path: "/admin/federation",      role: "super",  screen: () => import("@/screens/admin/Federation") },
  { path: "/admin/hooks",           role: "super",  screen: () => import("@/screens/admin/Hooks") },
  { path: "/admin/users",           role: "super",  screen: () => import("@/screens/admin/Users") },
];

/** `/` has no screen of its own. */
export const HOME_REDIRECT = { from: "/", to: "/chat" } as const;

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
  if (pathname.replace(/\/+$/, "") === "") return { kind: "redirect", to: HOME_REDIRECT.to };
  const pub = matchRoute(PUBLIC, pathname);
  if (pub) return { kind: "public", route: pub.route, params: pub.params };
  const auth = matchRoute(AUTHENTICATED, pathname);
  if (auth) return { kind: "authenticated", role: auth.route.role, route: auth.route, params: auth.params };
  return { kind: "authenticated", role: "member", route: null, params: {} };
}
