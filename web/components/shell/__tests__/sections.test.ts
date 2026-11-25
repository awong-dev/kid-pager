import { describe, expect, it } from "vitest";

import { initialsOf } from "../AccountMenu";
import { type Viewer, backTarget, bottomNavFor, isBottomItemActive, isThreadPath, sectionOf, visibleSections } from "../sections";

const member: Viewer = { isFamilyAdmin: false, isSuper: false };
const admin: Viewer = { isFamilyAdmin: true, isSuper: false };
const sup: Viewer = { isFamilyAdmin: true, isSuper: true };

const labels = (v: Viewer, path: string) => sectionOf(path, v).tabs.map((t) => t.label);

describe("sectionOf", () => {
  it("Home, account pages and unknown paths are in no section", () => {
    for (const p of ["/", "/account/pagers", "/account/notifications", "/devices/x", "/nope"]) {
      expect(sectionOf(p, sup).section, p).toBeNull();
    }
  });
  it("Messages is one dashboard: no tab row, and /location stays highlighted as part of it", () => {
    for (const v of [member, admin, sup]) {
      for (const p of ["/chat", "/location"]) {
        expect(sectionOf(p, v).section?.id, p).toBe("messages");
        expect(sectionOf(p, v).tabs, p).toEqual([]);
      }
    }
    expect(isBottomItemActive("messages", "/location", member)).toBe(true);
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
  it("Family has four tabs; group chats and settings highlight their own place", () => {
    expect(labels(admin, "/family/people")).toEqual(["People & contacts", "Permissions", "Pagers", "Settings"]);
    expect(sectionOf("/family/chat", admin)).toMatchObject({ section: { id: "family" }, activeTab: { label: "People & contacts" } });
    expect(sectionOf("/family/settings", admin).activeTab?.label).toBe("Settings");
    expect(sectionOf("/family/permissions", admin)).toMatchObject({
      section: { id: "family" },
      activeTab: { href: "/family/permissions" },
    });
    // the address book is an account page now, in no section
    expect(sectionOf("/account/book", admin).section).toBeNull();
  });
  it("a viewer is only placed in sections they may use", () => {
    expect(sectionOf("/account/book", member).section).toBeNull(); // everyone reaches it from the account menu
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

describe("backTarget", () => {
  it("threads and /location go back to the Messages dashboard; the dashboard itself has no back", () => {
    for (const p of ["/chat/bob", "/chat/view/k1", "/location"]) {
      expect(backTarget(p), p).toEqual({ href: "/chat", label: "Back to Messages" });
    }
    for (const p of ["/chat", "/", "/alerts", "/family/people"]) expect(backTarget(p), p).toBeNull();
  });
  it("/location keeps the bottom bar; threads hide it", () => {
    expect(isThreadPath("/location")).toBe(false);
    expect(isThreadPath("/chat/bob")).toBe(true);
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
