import { describe, expect, it } from "vitest";

import { anchorOf } from "../anchor";

describe("anchorOf", () => {
  it("reads a bare anchor with or without the #", () => {
    expect(anchorOf("#contacts")).toBe("contacts");
    expect(anchorOf("groups")).toBe("groups");
  });
  it("keeps the leading word of a compound fragment", () => {
    expect(anchorOf("#federation&link=abc")).toBe("federation");
  });
  it("is null for no fragment or a key=value first part", () => {
    expect(anchorOf("")).toBeNull();
    expect(anchorOf("#")).toBeNull();
    expect(anchorOf("#fedInvite=xyz")).toBeNull();
    expect(anchorOf("#&link=1")).toBeNull();
  });
  it("tolerates a bad escape", () => {
    expect(anchorOf("#100%")).toBe("100%");
  });
});
