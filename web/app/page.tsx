"use client";

/** The one page: a client-side router over the route table in `./routes.tsx`
 * (PUBLIC / AUTHENTICATED / API; read that file to see who can reach what).
 *
 * - public screen: rendered bare.
 * - authenticated screen (the default, also for unknown paths): `RequireAuth(role)` + `AppShell`.
 * - API path: never rendered; a full page load hands it to the relay.
 * The prerendered HTML is this page's loading screen; `usePathname()` is null until mount, so
 * the shell served for any URL hydrates identically. */

import { type ComponentType, Suspense, createElement, lazy, useEffect } from "react";

import AppShell from "@/components/AppShell";
import NotFound from "@/components/NotFound";
import RequireAuth, { LoadingScreen } from "@/components/RequireAuth";
import { ParamsProvider, useNavigate, usePathname } from "@/lib/router";

import { AUTHENTICATED, PUBLIC, type Screen, resolveRoute } from "./routes";

// One lazy component per screen loader (built once, at module load), so each screen stays its
// own chunk and loads on first visit.
const LAZY = new Map<Screen, ComponentType>(
  [...PUBLIC, ...AUTHENTICATED].map((r) => [r.screen, lazy(reloadOnMissingChunk(r.screen))]),
);

/** A tab left open across a deploy asks for a screen chunk that no longer exists: reload once to
 * pick up the new build, and fail normally if that does not help. */
function reloadOnMissingChunk(load: Screen): Screen {
  const key = "pager.chunkReload";
  return () =>
    load().then(
      (mod) => {
        sessionStorage.removeItem(key);
        return mod;
      },
      (err) => {
        if (sessionStorage.getItem(key) === window.location.pathname) throw err;
        sessionStorage.setItem(key, window.location.pathname);
        window.location.reload();
        return new Promise<never>(() => {});
      },
    );
}
function ScreenOf({ load }: { load: Screen }) {
  const Component = LAZY.get(load);
  return Component ? createElement(Component) : <NotFound />;
}

/** Reached an API path client-side (the relay should have answered it): hand it to the browser,
 * once, so a relay that is down cannot loop. */
function HandOffToRelay({ pathname }: { pathname: string }) {
  useEffect(() => {
    const url = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const key = "pager.apiHandOff";
    if (sessionStorage.getItem(key) === url) return;
    sessionStorage.setItem(key, url);
    window.location.assign(new URL(url, window.location.origin).href); // absolute: full page load
  }, [pathname]);
  return <LoadingScreen />;
}

function Redirect({ to }: { to: string }) {
  const navigate = useNavigate();
  useEffect(() => {
    navigate.replace(to);
  }, [navigate, to]);
  return <LoadingScreen />;
}

export default function App() {
  const pathname = usePathname();
  if (pathname === null) return <LoadingScreen />;

  const r = resolveRoute(pathname);
  switch (r.kind) {
    case "api":
      return <HandOffToRelay pathname={pathname} />;
    case "redirect":
      return <Redirect to={r.to} />;
    case "public":
      return (
        <ParamsProvider params={r.params}>
          <Suspense fallback={<LoadingScreen />}>
            <ScreenOf load={r.route.screen} />
          </Suspense>
        </ParamsProvider>
      );
    case "authenticated":
      return (
        <RequireAuth role={r.role}>
          <AppShell>
            <ParamsProvider params={r.params}>
              <Suspense fallback={<LoadingScreen />}>
                {r.route ? <ScreenOf load={r.route.screen} /> : <NotFound />}
              </Suspense>
            </ParamsProvider>
          </AppShell>
        </RequireAuth>
      );
  }
}
