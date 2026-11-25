import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { API, AUTHENTICATED, PUBLIC, resolveRoute } from "../../app/routes";
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
  it("`/` redirects to /chat and API paths never render", () => {
    expect(resolveRoute("/")).toEqual({ kind: "redirect", to: "/chat" });
    expect(resolveRoute("/api/x").kind).toBe("api");
    expect(resolveRoute("/webhooks/y").kind).toBe("api");
  });
  it("resolves the dynamic routes with params", () => {
    expect(resolveRoute("/chat/bob")).toMatchObject({ role: "member", params: { alias: "bob" } });
    expect(resolveRoute("/chat/view/k")).toMatchObject({ role: "admin", params: { key: "k" } });
    expect(resolveRoute("/devices/pgr-1")).toMatchObject({ role: "member", params: { id: "pgr-1" } });
  });
});
