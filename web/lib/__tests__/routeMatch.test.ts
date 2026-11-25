import { describe, expect, it } from "vitest";

import { matchPattern, matchRoute } from "../routeMatch";

describe("matchPattern", () => {
  it("captures and decodes params", () => {
    expect(matchPattern("/chat/:alias", "/chat/bob")).toEqual({ alias: "bob" });
    expect(matchPattern("/chat/:alias", "/chat/a%20b%2Fc")).toEqual({ alias: "a b/c" });
    expect(matchPattern("/chat/:alias", "/chat/100%")).toEqual({ alias: "100%" }); // bad escape kept
  });
  it("ignores a trailing slash", () => {
    expect(matchPattern("/chat", "/chat/")).toEqual({});
    expect(matchPattern("/chat/:alias", "/chat/bob/")).toEqual({ alias: "bob" });
  });
  it("needs the same number of segments", () => {
    expect(matchPattern("/chat/:alias", "/chat")).toBeNull();
    expect(matchPattern("/chat", "/chat/bob")).toBeNull();
    expect(matchPattern("/chat/:alias", "/chat/a/b")).toBeNull();
  });
  it("matches literals exactly", () => {
    expect(matchPattern("/admin/users", "/admin/user")).toBeNull();
    expect(matchPattern("/admin/users", "/Admin/users")).toBeNull();
  });
});

describe("matchRoute", () => {
  const routes = [
    { path: "/chat/:alias" },
    { path: "/chat/view/:key" },
    { path: "/chat/view" },
    { path: "/chat" },
  ];
  it("prefers the literal route regardless of table order", () => {
    expect(matchRoute(routes, "/chat/view")?.route.path).toBe("/chat/view");
    expect(matchRoute([...routes].reverse(), "/chat/view")?.route.path).toBe("/chat/view");
    expect(matchRoute(routes, "/chat/view/k1")).toEqual({ route: routes[1], params: { key: "k1" } });
    expect(matchRoute(routes, "/chat/bob")).toEqual({ route: routes[0], params: { alias: "bob" } });
  });
  it("returns null for an unknown path", () => {
    expect(matchRoute(routes, "/nope")).toBeNull();
    expect(matchRoute(routes, "/chat/a/b/c")).toBeNull();
  });
});
