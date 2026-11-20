import { describe, expect, it } from "vitest";

import { buildTemplate, looksLikeEmail, parseEmailList } from "../emailList";

describe("parseEmailList", () => {
  it("accepts comma, equals and whitespace separators and a leading @", () => {
    const r = parseEmailList("ann,ann@x.org\n@bo=bo@x.org\ncy   cy@x.org\n");
    expect(r.bad).toEqual([]);
    expect(r.entries.map((e) => [e.alias, e.email])).toEqual([
      ["ann", "ann@x.org"],
      ["bo", "bo@x.org"],
      ["cy", "cy@x.org"],
    ]);
  });
  it("treats - as no sign-in", () => {
    const r = parseEmailList("kid, -\nkid2 -");
    expect(r.entries.map((e) => e.email)).toEqual([null, null]);
  });
  it("ignores blanks and comments, numbers lines", () => {
    const r = parseEmailList("# hi\n\nann,a@x.org\nbad line here\n");
    expect(r.entries[0].line).toBe(3);
    expect(r.bad).toEqual([{ line: 4, text: "bad line here" }]);
  });
  it("skips template lines with no email and flags garbage", () => {
    const r = parseEmailList("ann,\n@bo\n,x@y.z\n");
    expect(r.entries).toEqual([]);
    expect(r.bad.map((b) => b.line)).toEqual([3]);
  });
  it("lowercases aliases", () => {
    expect(parseEmailList("Ann,a@x.org").entries[0].alias).toBe("ann");
  });
});

describe("helpers", () => {
  it("looksLikeEmail", () => {
    expect(looksLikeEmail("a@b.co")).toBe(true);
    expect(looksLikeEmail("a@b")).toBe(false);
    expect(looksLikeEmail("a b@c.d")).toBe(false);
  });
  it("buildTemplate", () => {
    expect(buildTemplate([{ alias: "a", email: "x@y.z" }, { alias: "b", email: "" }])).toBe("a,x@y.z\nb,\n");
  });
});
