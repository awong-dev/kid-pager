import { describe, expect, it } from "vitest";

import { CARDS, cardVisible, myPagerStatus, onlineLine, pagersStatus, plural, unreadStatus } from "../cards";

const dev = (state: string, revoked = false) =>
  ({ revokedAt: revoked ? ({} as never) : null, status: { state } }) as never;

const ids = (v: { isFamilyAdmin: boolean; isSuper: boolean }) =>
  CARDS.filter((c) => cardVisible(c, v)).map((c) => c.id);

describe("home cards", () => {
  it("who sees what", () => {
    expect(ids({ isFamilyAdmin: false, isSuper: false })).toEqual(["messages", "myPager", "notifications"]);
    expect(ids({ isFamilyAdmin: true, isSuper: false })).toEqual(["messages", "alerts", "people", "pagers", "history"]);
    expect(ids({ isFamilyAdmin: true, isSuper: true })).toEqual([
      "messages", "alerts", "people", "pagers", "history", "system",
    ]);
  });
  it("Messages is first and the only featured card", () => {
    expect(CARDS[0]!.id).toBe("messages");
    expect(CARDS.filter((c) => c.featured)).toHaveLength(1);
  });
});

describe("status lines", () => {
  it("counts", () => {
    expect(plural(1, "family member")).toBe("1 family member");
    expect(unreadStatus(3)).toBe("3 unread");
    expect(unreadStatus(0)).toBe("All caught up");
  });
  it("pagers skip revoked ones", () => {
    const ds = [dev("online"), dev("online"), dev("offline"), dev("online", true)];
    expect(onlineLine(ds)).toBe("2 of 3 pagers online");
    expect(pagersStatus(ds)).toBe("3 pagers, 1 offline");
    expect(pagersStatus([dev("online")])).toBe("1 pager");
    expect(pagersStatus([])).toBe("No pagers yet");
    expect(onlineLine([dev("online", true)])).toBeNull();
  });
  it("my pager", () => {
    const seen = () => 1000;
    const ago = (ms: number) => `${ms}ms ago`;
    expect(myPagerStatus([], seen, ago)).toBe("No pager linked");
    expect(myPagerStatus([dev("online")], seen, ago)).toBe("Online");
    expect(myPagerStatus([dev("offline")], seen, ago)).toBe("Last seen 1000ms ago");
    expect(myPagerStatus([dev("offline")], () => null, ago)).toBe("Offline");
  });
});
