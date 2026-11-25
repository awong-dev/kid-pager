import { describe, expect, it } from "vitest";

import { groupOwners, subscribedLine } from "../groupChatsModel";

const b = (ownerUid: string, ownerName: string | null, paired: boolean, ownerAlias: string | null = null) => ({
  ownerUid,
  ownerName,
  ownerAlias,
  paired,
});

describe("groupOwners", () => {
  it("is one row per owner with phone and paired counts, sorted by name", () => {
    expect(groupOwners([b("u2", "Zed", true), b("u1", "Amy", false), b("u2", "Zed", false)])).toEqual([
      { uid: "u1", name: "Amy", alias: null, phones: 1, paired: 0 },
      { uid: "u2", name: "Zed", alias: null, phones: 2, paired: 1 },
    ]);
  });
  it("falls back to the alias, then the uid, for the name", () => {
    expect(groupOwners([b("u1", null, true, "amy")])[0]!.name).toBe("@amy");
    expect(groupOwners([b("u1", null, true)])[0]!.name).toBe("u1");
  });
  it("is empty with no bridge phones", () => {
    expect(groupOwners([])).toEqual([]);
  });
});

describe("subscribedLine", () => {
  it("says what is known", () => {
    expect(subscribedLine({ paired: 0 }, 5)).toBe("Phone not paired yet");
    expect(subscribedLine({ paired: 1 }, null)).toBeNull();
    expect(subscribedLine({ paired: 1 }, 0)).toBe("Nothing subscribed yet");
    expect(subscribedLine({ paired: 2 }, 3)).toBe("3 subscribed");
  });
});
