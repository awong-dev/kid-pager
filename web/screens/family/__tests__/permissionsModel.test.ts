import { describe, expect, it } from "vitest";

import {
  NEW_PERSON_DEFAULTS,
  type ApprovedData,
  approvedLine,
  locateViewers,
  newPersonLine,
  permissionsHref,
  rulesLine,
  selectedPerson,
  tallyLines,
  whoOf,
} from "../permissionsModel";

const data = (people: ApprovedData["people"], contacts: ApprovedData["contacts"] = []): ApprovedData => ({
  people,
  contacts,
});

describe("?who= selection", () => {
  const people = [{ alias: "kid" }, { alias: "mum" }];
  it("reads the alias from the query string", () => {
    expect(whoOf("?who=kid")).toBe("kid");
    expect(whoOf("?x=1&who=mum")).toBe("mum");
    expect(whoOf("")).toBeNull();
    expect(whoOf("?who=")).toBeNull();
    expect(whoOf("?who=%20")).toBeNull();
  });
  it("picks the listed person it names, else nobody", () => {
    expect(selectedPerson(people, "?who=mum")).toBe(people[1]);
    expect(selectedPerson(people, "?who=ghost")).toBeNull();
    expect(selectedPerson(people, "")).toBeNull();
  });
  it("builds links that round-trip", () => {
    expect(permissionsHref()).toBe("/family/permissions");
    expect(permissionsHref("kid")).toBe("/family/permissions?who=kid");
    const href = permissionsHref("a b&c");
    expect(whoOf(href.slice(href.indexOf("?")))).toBe("a b&c");
  });
});

describe("summary text", () => {
  it("states the rules in words", () => {
    expect(rulesLine({ out: "people", in: "any" })).toBe("Can message: People. Can be messaged by: Anyone.");
    expect(rulesLine({ out: "weird", in: "people_sms" })).toBe(
      "Can message: weird. Can be messaged by: People + phone numbers.",
    );
  });
  it("counts only approved (message) entries", () => {
    const d = data(
      [
        { alias: "a", message: true, locate: false },
        { alias: "b", message: false, locate: false },
        { alias: "c", message: true, locate: true },
      ],
      [{ uid: "x", message: true }, { uid: "y", message: false }],
    );
    expect(approvedLine(d, "people")).toBe("2 people, 1 contact approved");
    expect(approvedLine(data([{ alias: "a", message: true, locate: false }]), "people")).toBe("1 person approved");
    expect(approvedLine(data([]), "people")).toBe("No one approved yet");
    expect(approvedLine(data([]), "open")).toBe("No one approved yet; every contact is included (Open)");
  });
  it("tallies policies across the family", () => {
    expect(tallyLines([])).toEqual([]);
    expect(
      tallyLines([
        { out: "people", in: "people" },
        { out: "open", in: "any" },
        { out: "people", in: "people" },
      ]),
    ).toEqual(["Who they can message: 2 People, 1 Open", "Who can message them: 2 People, 1 Anyone"]);
  });
});

describe("who can see someone's location", () => {
  const byOwner = {
    mum: data([{ alias: "kid", message: true, locate: true }]),
    dad: data([{ alias: "kid", message: true, locate: false }]),
    gran: data([{ alias: "kid", message: false, locate: true }]),
    kid: data([{ alias: "kid", message: true, locate: true }]),
    nan: undefined,
  };
  it("lists owners whose approved list has Locate on for them", () => {
    expect(locateViewers(byOwner, "kid")).toEqual(["mum"]);
    expect(locateViewers(byOwner, "mum")).toEqual([]);
  });
});

describe("new person defaults", () => {
  it("match the relay's per-role defaults and are described in the page's words", () => {
    expect(NEW_PERSON_DEFAULTS.member).toEqual({ out: "people", in: "people" });
    expect(NEW_PERSON_DEFAULTS.admin).toEqual({ out: "open", in: "any" });
    expect(newPersonLine()).toContain('A new member can message "People"');
    expect(newPersonLine()).toContain('A new admin can message "Open" and can be messaged by "Anyone"');
  });
});
