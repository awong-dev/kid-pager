import { beforeEach, describe, expect, it } from "vitest";

import { isSafeReturnPath, stashReturnPath, takeReturnPath } from "@/lib/returnPath";

describe("isSafeReturnPath", () => {
  it("accepts same-origin relative paths", () => {
    expect(isSafeReturnPath("/chat")).toBe(true);
    expect(isSafeReturnPath("/family/people#fedInvite=abc")).toBe(true);
    expect(isSafeReturnPath("/a/b?x=1#y")).toBe(true);
  });
  it("rejects anything else", () => {
    for (const p of ["", "chat", "//evil.com", "/\\evil.com", "https://evil.com", "javascript:alert(1)", "/a\nb", "/a\\b", null, 5]) {
      expect(isSafeReturnPath(p)).toBe(false);
    }
  });
});

describe("stash / take", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { sessionStorage?: unknown }).sessionStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });
  it("returns the stashed path once, then /chat", () => {
    stashReturnPath("/family/people#fedInvite=x");
    expect(takeReturnPath()).toBe("/family/people#fedInvite=x");
    expect(takeReturnPath()).toBe("/chat");
  });
  it("ignores unsafe and login paths", () => {
    stashReturnPath("//evil.com");
    expect(takeReturnPath()).toBe("/chat");
    stashReturnPath("/login");
    expect(takeReturnPath()).toBe("/chat");
  });
});
