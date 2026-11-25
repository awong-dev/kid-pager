/** The navigation table: ONE place that says what the top links, the section tab rows and the
 * phone bottom bar contain, and which section a path belongs to. To add, move or rename a page
 * in the menus, edit `SECTIONS` (or `HOME` / `MY_PAGER`) below; nothing else lists nav items.
 * Pure data and functions (no React) so `components/shell/__tests__` can check them. */

import { matchPattern } from "@/lib/routeMatch";

export type Access = "member" | "admin" | "super";

/** What the shell needs to know about the viewer. */
export interface Viewer {
  isFamilyAdmin: boolean;
  isSuper: boolean;
}

export interface Tab {
  href: string;
  label: string;
  /** Hidden unless the viewer passes this; default member. */
  access?: Access;
}

export interface Section {
  id: "messages" | "alerts" | "family" | "system";
  label: string;
  /** Where the nav link goes. */
  href: string;
  access: Access;
  /** Path prefixes (whole segments) that belong to this section. */
  prefixes: readonly string[];
  /** Sub-pages, shown as a tab row while inside the section. Empty = no tab row. */
  tabs: readonly Tab[];
}

export const HOME = { id: "home", label: "Home", href: "/" } as const;
/** A member who is not an admin gets this third bottom-bar item. */
export const MY_PAGER = { id: "pager", label: "My pager", href: "/account/pagers" } as const;

export const SECTIONS: readonly Section[] = [
  {
    id: "messages", label: "Messages", href: "/chat", access: "member",
    // One dashboard (`/chat`) holds conversations, locations and the admin audit list, so no tabs;
    // `/location` is its detail screen and stays highlighted here.
    prefixes: ["/chat", "/location"],
    tabs: [],
  },
  {
    id: "alerts", label: "Alerts", href: "/alerts", access: "admin",
    prefixes: ["/alerts"],
    tabs: [],
  },
  {
    id: "family", label: "Family", href: "/family/people", access: "admin",
    prefixes: ["/family", "/settings/book"],
    tabs: [
      { href: "/family/people",   label: "People" },
      { href: "/family/contacts", label: "Contacts" },
      { href: "/family/chat",     label: "Group chats" },
      { href: "/settings/book",   label: "Address books" },
      { href: "/family/devices",  label: "Pagers" },
      { href: "/family/history",  label: "Message history" },
    ],
  },
  {
    id: "system", label: "System", href: "/admin/families", access: "super",
    prefixes: ["/admin"],
    tabs: [
      { href: "/admin/families",   label: "Families" },
      { href: "/admin/users",      label: "All users" },
      { href: "/admin/devices",    label: "All devices" },
      { href: "/admin/allowlist",  label: "Allow-list" },
      { href: "/admin/hooks",      label: "Hooks" },
      { href: "/admin/federation", label: "Federation" },
    ],
  },
];

/** Chat threads own the whole screen: no tab row, no bottom bar, a back arrow instead. */
export const THREAD_PATTERNS: readonly string[] = ["/chat/:alias", "/chat/view/:key"];

export function isThreadPath(pathname: string): boolean {
  return THREAD_PATTERNS.some((p) => matchPattern(p, pathname) !== null);
}

/** Screens reached from the Messages dashboard get a way back to it: a back arrow in the phone
 * top bar (and, on `/location`, a link on desktop). Threads also hide the bottom bar; `/location`
 * keeps it. */
export const BACK_TO_MESSAGES: readonly { pattern: string; label: string }[] = [
  { pattern: "/chat/:alias",     label: "Back to Messages" },
  { pattern: "/chat/view/:key",  label: "Back to Messages" },
  { pattern: "/location",        label: "Back to Messages" },
];

/** Where the phone bar's back arrow goes from `pathname`, or null when it shows none. */
export function backTarget(pathname: string): { href: string; label: string } | null {
  const hit = BACK_TO_MESSAGES.find((b) => matchPattern(b.pattern, pathname) !== null);
  return hit ? { href: "/chat", label: hit.label } : null;
}

export function allows(access: Access, v: Pick<Viewer, "isFamilyAdmin" | "isSuper">): boolean {
  return access === "member" || (access === "admin" ? v.isFamilyAdmin : v.isSuper);
}

/** `path` is `prefix` or below it (whole segments: "/family" is not "/familyx"). */
export function under(path: string, prefix: string): boolean {
  const p = path.replace(/(?<=.)\/+$/, "");
  return p === prefix || p.startsWith(`${prefix}/`);
}

/** Sections the viewer may see, in nav order. */
export function visibleSections(v: Viewer): Section[] {
  return SECTIONS.filter((s) => allows(s.access, v));
}

/** The tabs of `section` this viewer sees. */
export function visibleTabs(section: Section, v: Viewer): Tab[] {
  return section.tabs.filter((t) => allows(t.access ?? "member", v));
}

/** Which section (and tab) a path is in for this viewer. `null` section: Home, the account pages,
 * Not Found, or a section the viewer cannot use. `tabs` is empty when no row should show (a thread,
 * or fewer than two tabs). */
export function sectionOf(
  pathname: string,
  v: Viewer,
): { section: Section | null; tabs: Tab[]; activeTab: Tab | null } {
  const none = { section: null, tabs: [], activeTab: null };
  const section = visibleSections(v).find((s) => s.prefixes.some((p) => under(pathname, p)));
  if (!section) return none;
  const all = visibleTabs(section, v);
  // the longest matching tab href wins
  const activeTab =
    all.filter((t) => under(pathname, t.href)).sort((a, b) => b.href.length - a.href.length)[0] ?? null;
  const showRow = all.length >= 2 && !isThreadPath(pathname);
  return { section, tabs: showRow ? all : [], activeTab };
}

/** Phone bottom bar: Home + the sections the viewer can use; a plain member gets My pager. */
export function bottomNavFor(v: Viewer): { id: string; label: string; href: string }[] {
  const items: { id: string; label: string; href: string }[] = [HOME];
  const sections = visibleSections(v);
  items.push(...sections.map(({ id, label, href }) => ({ id, label, href })));
  if (!v.isFamilyAdmin) items.push(MY_PAGER);
  return items;
}

/** Whether a bottom-bar item is the current one (Home only on exactly "/"). */
export function isBottomItemActive(id: string, pathname: string, v: Viewer): boolean {
  if (id === HOME.id) return pathname.replace(/(?<=.)\/+$/, "") === "/";
  if (id === MY_PAGER.id) return under(pathname, MY_PAGER.href);
  return sectionOf(pathname, v).section?.id === id;
}
