import { describe, expect, it } from "vitest";

import { initialsOf } from "../AccountMenu";
import { type Viewer, bottomNavFor, isBottomItemActive, isThreadPath, sectionOf, visibleSections } from "../sections";

const member: Viewer = { isFamilyAdmin: false, isSuper: false, showLocation: false };
const memberLoc: Viewer = { ...member, showLocation: true };
const admin: Viewer = { isFamilyAdmin: true, isSuper: false, showLocation: true };
const sup: Viewer = { isFamilyAdmin: true, isSuper: true, showLocation: true };

const labels = (v: Viewer, path: string) => sectionOf(path, v).tabs.map((t) => t.label);

describe("sectionOf", () => {
  it("Home, account pages and unknown paths are in no section", () => {
    for (const p of ["/", "/account/pagers", "/account/notifications", "/devices/x", "/nope"]) {
      expect(sectionOf(p, sup).section, p).toBeNull();
    }
  });
  it("Messages: Chats | Location, Location only when the viewer has devices", () => {
    expect(sectionOf("/chat", memberLoc).section?.id).toBe("messages");
    expect(labels(memberLoc, "/chat")).toEqual(["Chats", "Location"]);
    expect(labels(member, "/chat")).toEqual([]); // a lone tab is not a row
    expect(sectionOf("/location", memberLoc).activeTab?.href).toBe("/location");
    expect(sectionOf("/chat", memberLoc).activeTab?.href).toBe("/chat");
  });
  it("threads show no tab row but stay in Messages", () => {
    for (const p of ["/chat/bob", "/chat/view/k1"]) {
      expect(isThreadPath(p)).toBe(true);
      expect(sectionOf(p, admin).section?.id).toBe("messages");
      expect(sectionOf(p, admin).tabs).toEqual([]);
    }
    expect(isThreadPath("/chat")).toBe(false);
    expect(isThreadPath("/chat/a/b/c")).toBe(false);
  });
  it("Family has six tabs including Address books and Message history", () => {
    expect(labels(admin, "/family/people")).toEqual([
      "People", "Contacts", "Group chats", "Address books", "Pagers", "Message history",
    ]);
    expect(sectionOf("/settings/book", admin)).toMatchObject({
      section: { id: "family" },
      activeTab: { href: "/settings/book" },
    });
    expect(sectionOf("/family/history", admin).activeTab?.label).toBe("Message history");
  });
  it("a viewer is only placed in sections they may use", () => {
    expect(sectionOf("/settings/book", member).section).toBeNull(); // members reach it from the account menu
    expect(sectionOf("/family/people", member).section).toBeNull();
    expect(sectionOf("/admin/users", admin).section).toBeNull();
    expect(sectionOf("/admin/users", sup).section?.id).toBe("system");
    expect(labels(sup, "/admin/hooks")).toEqual(["Families", "All users", "All devices", "Allow-list", "Hooks", "Federation"]);
  });
  it("Alerts is a section without tabs", () => {
    expect(sectionOf("/alerts", admin)).toMatchObject({ section: { id: "alerts" }, tabs: [] });
    expect(sectionOf("/alerts", member).section).toBeNull();
  });
  it("matches whole path segments", () => {
    expect(sectionOf("/familyx", admin).section).toBeNull();
    expect(sectionOf("/family/", admin).section?.id).toBe("family");
  });
});

describe("navigation lists", () => {
  it("top links by role", () => {
    expect(visibleSections(member).map((s) => s.label)).toEqual(["Messages"]);
    expect(visibleSections(admin).map((s) => s.label)).toEqual(["Messages", "Alerts", "Family"]);
    expect(visibleSections(sup).map((s) => s.label)).toEqual(["Messages", "Alerts", "Family", "System"]);
  });
  it("bottom bar by role", () => {
    expect(bottomNavFor(member).map((i) => i.label)).toEqual(["Home", "Messages", "My pager"]);
    expect(bottomNavFor(admin).map((i) => i.label)).toEqual(["Home", "Messages", "Alerts", "Family"]);
    expect(bottomNavFor(sup).map((i) => i.label)).toEqual(["Home", "Messages", "Alerts", "Family", "System"]);
  });
  it("bottom bar highlights the current item; Home only on /", () => {
    expect(isBottomItemActive("home", "/", sup)).toBe(true);
    expect(isBottomItemActive("home", "/chat", sup)).toBe(false);
    expect(isBottomItemActive("messages", "/chat/bob", sup)).toBe(true);
    expect(isBottomItemActive("pager", "/account/pagers", member)).toBe(true);
    expect(isBottomItemActive("family", "/account/pagers", admin)).toBe(false);
  });
});

describe("initialsOf", () => {
  it("takes first and last word", () => {
    expect(initialsOf("Ada Lovelace")).toBe("AL");
    expect(initialsOf("ada")).toBe("A");
    expect(initialsOf("  ")).toBe("?");
    expect(initialsOf("Jean Luc Picard")).toBe("JP");
  });
});
