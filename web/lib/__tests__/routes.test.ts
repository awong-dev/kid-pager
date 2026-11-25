import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { API, AUTHENTICATED, PUBLIC, REDIRECTS, resolveRoute } from "../../app/routes";
import { CARDS } from "../../screens/home/cards";
import { HOME, MY_PAGER, SECTIONS } from "../../components/shell/sections";
import { API_PATH_PREFIXES, isApiPath } from "../apiPaths";

type Rewrite = { source: string; run?: unknown; destination?: string };
// The repo-root file is what `firebase deploy` uses; web/firebase.json is the local preview.
const configs = ["../../../firebase.json", "../../firebase.json"].map((rel) => ({
  rel,
  rewrites: (
    JSON.parse(readFileSync(join(__dirname, rel), "utf8")) as { hosting: { rewrites: Rewrite[] } }
  ).hosting.rewrites,
}));

describe("API paths", () => {
  it.each(configs)("$rel relay rewrites list exactly lib/apiPaths.ts", ({ rewrites }) => {
    const relay = rewrites.filter((r) => r.run).map((r) => r.source);
    expect(relay.sort()).toEqual(API_PATH_PREFIXES.map((p) => `${p}/**`).sort());
    expect([...API]).toEqual([...API_PATH_PREFIXES]);
  });
  it.each(configs)("$rel relay rewrites come before the catch-all, which is last", ({ rewrites }) => {
    const last = rewrites[rewrites.length - 1]!;
    expect(last).toEqual({ source: "**", destination: "/index.html" });
    expect(rewrites.slice(0, -1).every((r) => r.run)).toBe(true);
  });
  it("matches prefixes on segment boundaries", () => {
    expect(isApiPath("/api/me")).toBe(true);
    expect(isApiPath("/webhooks")).toBe(true);
    expect(isApiPath("/apiary")).toBe(false);
  });
});

describe("route table", () => {
  it("only PUBLIC routes resolve as public", () => {
    for (const r of PUBLIC) expect(resolveRoute(r.path).kind).toBe("public");
    for (const r of AUTHENTICATED) {
      const path = r.path.replace(/:\w+/g, "x");
      expect(resolveRoute(path).kind, path).toBe("authenticated");
    }
  });
  it("unknown paths are authenticated (default), never public", () => {
    for (const p of ["/nope", "/admin", "/login/extra", "/chat/a/b/c", "/Login", "/devices"]) {
      expect(resolveRoute(p)).toMatchObject({ kind: "authenticated", role: "member", route: null });
    }
  });
  it("each authenticated route carries the role listed in its row", () => {
    for (const r of AUTHENTICATED) {
      expect(["member", "admin", "super"]).toContain(r.role);
      expect(resolveRoute(r.path.replace(/:\w+/g, "x"))).toMatchObject({ role: r.role, route: r });
    }
  });
  it("no path is listed twice or in both blocks", () => {
    const paths = [...PUBLIC, ...AUTHENTICATED].map((r) => r.path);
    expect(new Set(paths).size).toBe(paths.length);
  });
  it("`/` is the member Home screen and API paths never render", () => {
    expect(resolveRoute("/")).toMatchObject({ kind: "authenticated", role: "member" });
    expect(resolveRoute("/")).toMatchObject({ route: { path: "/" } });
    expect(resolveRoute("/api/x").kind).toBe("api");
    expect(resolveRoute("/webhooks/y").kind).toBe("api");
  });
  it("resolves the dynamic routes with params", () => {
    expect(resolveRoute("/chat/bob")).toMatchObject({ role: "member", params: { alias: "bob" } });
    expect(resolveRoute("/chat/view/k")).toMatchObject({ role: "admin", params: { key: "k" } });
    expect(resolveRoute("/devices/pgr-1")).toMatchObject({ role: "member", params: { id: "pgr-1" } });
  });
});

const RANK = { member: 0, admin: 1, super: 2 } as const;
const roleOf = (path: string): number => {
  const r = resolveRoute(path);
  if (r.kind !== "authenticated" || !r.route) throw new Error(`${path} is not a listed route`);
  return RANK[r.role];
};
/** Every href a user can click that is not a route pattern: all must resolve to a listed route. */
const navHrefs: { where: string; href: string }[] = [
  { where: "home", href: HOME.href },
  { where: "my pager", href: MY_PAGER.href },
  ...SECTIONS.flatMap((s) => [
    { where: `${s.id} link`, href: s.href },
    ...s.tabs.map((t) => ({ where: `${s.id} tab`, href: t.href })),
  ]),
  ...CARDS.map((c) => ({ where: `card ${c.id}`, href: c.href })),
  ...["/account/notifications", "/account/apps", "/account/pagers", "/account/book"].map((href) => ({
    where: "account menu",
    href,
  })),
];

describe("redirects", () => {
  it("old paths redirect to their new home", () => {
    for (const r of REDIRECTS) expect(resolveRoute(r.from)).toEqual({ kind: "redirect", to: r.to });
    expect(resolveRoute("/family/alerts/")).toEqual({ kind: "redirect", to: "/alerts" });
    expect(resolveRoute("/admin/settings")).toEqual({ kind: "redirect", to: "/family/settings" });
    expect(resolveRoute("/family/history")).toEqual({ kind: "redirect", to: "/family/settings" });
    expect(resolveRoute("/family/contacts")).toEqual({ kind: "redirect", to: "/family/people#contacts" });
    expect(resolveRoute("/settings/book")).toEqual({ kind: "redirect", to: "/account/book" });
  });
  it("every target is a listed route that is at least as strict as the old path was", () => {
    // The old roles: alerts/retention were admin-only, the rest member.
    const was: Record<string, number> = {
      "/family/alerts": RANK.admin,
      "/admin/settings": RANK.admin,
      "/family/contacts": RANK.admin,
      "/family/history": RANK.admin,
      "/settings/backends": RANK.member,
      "/settings/devices": RANK.member,
      "/settings/notifications": RANK.member,
      "/settings/book": RANK.member,
    };
    expect(Object.keys(was).sort()).toEqual(REDIRECTS.map((r) => r.from).sort());
    for (const r of REDIRECTS) expect(roleOf(r.to.replace(/#.*/, "")), r.to).toBeGreaterThanOrEqual(was[r.from]!);
  });
  it("a source is never a route, a target, or another redirect's source twice", () => {
    const paths = AUTHENTICATED.map((r) => r.path);
    for (const r of REDIRECTS) {
      expect(paths).not.toContain(r.from);
      expect(REDIRECTS.map((x) => x.from)).not.toContain(r.to);
    }
    expect(new Set(REDIRECTS.map((r) => r.from)).size).toBe(REDIRECTS.length);
  });
});

describe("navigation", () => {
  it("every nav, tab and home-card href is a listed route", () => {
    for (const { where, href } of navHrefs) {
      const r = resolveRoute(href);
      expect(r.kind, `${where}: ${href}`).toBe("authenticated");
      expect(r.kind === "authenticated" && r.route, `${where}: ${href}`).toBeTruthy();
    }
  });
  it("the new family pages are admin routes and the retired ones are gone from the table", () => {
    for (const p of ["/family/people", "/family/permissions", "/family/settings", "/family/chat"]) {
      expect(roleOf(p), p).toBe(RANK.admin);
    }
    expect(roleOf("/account/book")).toBe(RANK.member);
    expect(AUTHENTICATED.map((r) => r.path)).not.toContain("/settings/book");
    const paths = AUTHENTICATED.map((r) => r.path);
    expect(paths).not.toContain("/family/contacts");
    expect(paths).not.toContain("/family/history");
  });
  it("no nav href is a redirect source", () => {
    const sources = REDIRECTS.map((r) => r.from);
    for (const { where, href } of navHrefs) expect(sources, `${where}: ${href}`).not.toContain(href);
  });
  it("nobody is offered a link their role cannot open", () => {
    for (const s of SECTIONS) {
      expect(roleOf(s.href), s.href).toBeLessThanOrEqual(RANK[s.access]);
      for (const t of s.tabs) expect(roleOf(t.href), t.href).toBeLessThanOrEqual(RANK[t.access ?? s.access]);
    }
    const audienceRank = { everyone: 0, members: 0, admin: 1, super: 2 } as const;
    for (const c of CARDS) expect(roleOf(c.href), c.id).toBeLessThanOrEqual(audienceRank[c.audience]);
  });
});
