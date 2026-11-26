import { afterEach, describe, expect, it, vi } from "vitest";

import { basePath, isUnderBase, resetBasePathCache, serverUrl, stripBase, withBase } from "../basePath";

/** The prefix lives in `<html data-pager-base>`; fake just enough of `document` to read it. */
function withPrefix(value: string | null) {
  resetBasePathCache();
  vi.stubGlobal("document", {
    documentElement: { getAttribute: (n: string) => (n === "data-pager-base" && value !== null ? value : null) },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBasePathCache();
});

describe("no prefix", () => {
  it("is the identity", () => {
    withPrefix("");
    expect(basePath()).toBe("");
    expect(withBase("/chat?x=1#y")).toBe("/chat?x=1#y");
    expect(stripBase("/chat")).toBe("/chat");
    expect(isUnderBase("/anything")).toBe(true);
  });

  it("treats a missing attribute as no prefix", () => {
    withPrefix(null);
    expect(basePath()).toBe("");
  });
});

describe("prefix /pager", () => {
  it("normalises the value", () => {
    withPrefix("/pager/");
    expect(basePath()).toBe("/pager");
    withPrefix("pager");
    expect(basePath()).toBe("/pager");
  });

  it("withBase prefixes only absolute paths", () => {
    withPrefix("/pager");
    expect(withBase("/chat/a%20b?x=1#y")).toBe("/pager/chat/a%20b?x=1#y");
    expect(withBase("/")).toBe("/pager/");
    expect(withBase("https://example.com/x")).toBe("https://example.com/x");
    expect(withBase("//example.com/x")).toBe("//example.com/x");
    expect(withBase("#frag")).toBe("#frag");
    expect(withBase("?q=1")).toBe("?q=1");
  });

  it("stripBase inverts withBase", () => {
    withPrefix("/pager");
    expect(stripBase("/pager/chat")).toBe("/chat");
    expect(stripBase("/pager/")).toBe("/");
    expect(stripBase("/pager")).toBe("/");
    expect(stripBase("/pagerx/chat")).toBe("/pagerx/chat");
    expect(stripBase("/other")).toBe("/other");
  });

  it("isUnderBase", () => {
    withPrefix("/pager");
    expect(isUnderBase("/pager")).toBe(true);
    expect(isUnderBase("/pager/a")).toBe(true);
    expect(isUnderBase("/pagerx")).toBe(false);
    expect(isUnderBase("/api/x")).toBe(false);
  });

  it("serverUrl is origin plus prefix", () => {
    withPrefix("/pager");
    vi.stubGlobal("window", { location: { origin: "https://host" } });
    expect(serverUrl()).toBe("https://host/pager");
  });
});
