import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import {
  fedDeliveryLabel,
  fedErrorText,
  fedMovedInfo,
  fedReauthInfo,
  formatFedServer,
  isPassphraseRequired,
  linkStateLabel,
  parseFedInviteHash,
  parseFedLinkHash,
  reauthConfirmDetail,
  reportFederationLines,
  shortFingerprint,
} from "@/lib/federation";

const err = (status: number, detail: unknown) => new ApiError(status, detail);

describe("shortFingerprint", () => {
  it("keeps short ones and truncates long ones", () => {
    expect(shortFingerprint("AB12 CD34")).toBe("AB12 CD34");
    expect(shortFingerprint("AB12 CD34 EF56 GH78 IJ90 KL12")).toBe("AB12 CD34 EF56 GH78…");
  });
});

describe("fedErrorText", () => {
  it("maps 502 details", () => {
    expect(fedErrorText(err(502, "address_not_allowed"))).toBe("That server's address is not allowed from here.");
    expect(fedErrorText(err(502, "unreachable"))).toMatch(/could not be reached/);
    expect(fedErrorText(err(502, "weird"))).toMatch(/Could not reach/);
  });
  it("maps 400 and 409", () => {
    expect(fedErrorText(err(400, "bad_invite"))).toMatch(/not valid/);
    expect(fedErrorText(err(409, "already linked"))).toMatch(/already linked/);
    expect(fedErrorText(err(409, "that invite is from this family"))).toMatch(/own family/);
    expect(fedErrorText(err(409, "link limit reached"))).toMatch(/16/);
    expect(fedErrorText(err(409, "reauthorize this link first"))).toMatch(/Re-authorize/);
    expect(fedErrorText(err(409, "something else"))).toMatch(/cannot be made/);
  });
  it("falls back for non-API errors", () => {
    expect(fedErrorText(new Error("x"), "nope")).toBe("nope");
  });
});

describe("linkStateLabel", () => {
  it("is offline only when active and down", () => {
    expect(linkStateLabel({ state: "active", health: { state: "ok", since: 0 } })).toBe("active");
    expect(linkStateLabel({ state: "active", health: { state: "down", since: 1 } })).toBe("offline");
    expect(linkStateLabel({ state: "suspended", health: { state: "down", since: 1 } })).toBe("suspended");
  });
});

describe("fedDeliveryLabel", () => {
  it("reads server and serverHost", () => {
    expect(fedDeliveryLabel({ server: "Smiths", serverHost: "h.example" })).toEqual({ name: "Smiths", host: "h.example" });
    expect(fedDeliveryLabel({}).name).toBeUndefined();
  });
});

describe("fedMovedInfo", () => {
  it("builds the sentence from fields", () => {
    const r = fedMovedInfo({ fedName: "Smiths", fedOldHost: "a.example", fedHost: "b.example", fedLinkId: "l1" });
    expect(r.text).toBe("Smiths moved from a.example to b.example. Suspend the link if you did not expect this.");
    expect(r.linkId).toBe("l1");
  });
});

describe("isPassphraseRequired", () => {
  it("matches a 400 mentioning the passphrase", () => {
    expect(isPassphraseRequired(err(400, "the X-Export-Passphrase header is required"))).toBe(true);
    expect(isPassphraseRequired(err(409, "passphrase"))).toBe(false);
    expect(isPassphraseRequired(err(400, "nope"))).toBe(false);
  });
});

describe("parseFedInviteHash", () => {
  it("reads the invite from the fragment", () => {
    expect(parseFedInviteHash("#fedInvite=abc.def")).toBe("abc.def");
    expect(parseFedInviteHash("fedInvite=a%2Bb")).toBe("a+b");
    expect(parseFedInviteHash("#x=1&fedInvite=zz")).toBe("zz");
  });
  it("ignores other fragments", () => {
    expect(parseFedInviteHash("")).toBeNull();
    expect(parseFedInviteHash("#federation")).toBeNull();
    expect(parseFedInviteHash("#fedInvite=")).toBeNull();
  });
});

describe("reauthConfirmDetail", () => {
  const link = { id: "l1", remote: { fedId: "f", name: "Smiths", host: "old.example", port: 443, path: "", fingerprint: "AA" } };
  it("recognises the reauth_confirm 409", () => {
    const r = reauthConfirmDetail(
      err(409, { code: "reauth_confirm", link, newFingerprint: "BB", newHost: "new.example", newPort: 8443 })
    );
    expect(r?.link.remote.name).toBe("Smiths");
    expect(r?.newHost).toBe("new.example");
    expect(r?.newPort).toBe(8443);
  });
  it("rejects other errors", () => {
    expect(reauthConfirmDetail(err(409, "already linked"))).toBeNull();
    expect(reauthConfirmDetail(err(409, { code: "other", link }))).toBeNull();
    expect(reauthConfirmDetail(err(400, { code: "reauth_confirm", link }))).toBeNull();
    expect(reauthConfirmDetail(new Error("x"))).toBeNull();
    expect(reauthConfirmDetail(err(409, { code: "reauth_confirm" }))).toBeNull();
  });
});

describe("reportFederationLines", () => {
  it("renders typed counts", () => {
    const r = reportFederationLines({ links: 3, reauth: 2, announcing: 1 });
    expect(r.reauth).toMatch(/^2 links to other families need re-authorizing/);
    expect(r.announcing).toBe(true);
    expect(reportFederationLines({ links: 1, reauth: 1, announcing: 0 }).reauth).toMatch(/^1 link to/);
    expect(reportFederationLines({ links: 0, reauth: 0, announcing: 0 })).toEqual({ reauth: null, announcing: false });
  });
  it("renders nothing without a federation part", () => {
    expect(reportFederationLines(undefined)).toEqual({ reauth: null, announcing: false });
  });
});

describe("fedReauthInfo and link state", () => {
  it("builds the alert text", () => {
    expect(fedReauthInfo({ fedName: "Smiths", fedHost: "h.example", fedLinkId: "l1" })).toEqual({
      text: "Smiths was restored from a backup and re-linked from h.example.",
      linkId: "l1",
    });
  });
  it("labels reauth", () => {
    expect(linkStateLabel({ state: "reauth", health: { state: "down", since: 0 } })).toBe("reauth");
  });
});

describe("parseFedLinkHash", () => {
  it("reads the link id", () => {
    expect(parseFedLinkHash("#federation&link=fam_abc")).toBe("fam_abc");
    expect(parseFedLinkHash("#federation")).toBeNull();
  });
});

describe("formatFedServer", () => {
  it("omits port 443 and an empty path", () => {
    expect(formatFedServer("example.org", 443, "")).toBe("example.org");
  });
  it("appends the path", () => {
    expect(formatFedServer("example.org", 443, "/kid-pager")).toBe("example.org/kid-pager");
  });
  it("shows a non-443 port before the path", () => {
    expect(formatFedServer("example.org", 8443, "/kid-pager")).toBe("example.org:8443/kid-pager");
  });
  it("tolerates missing port and path", () => {
    expect(formatFedServer("example.org")).toBe("example.org");
  });
});
