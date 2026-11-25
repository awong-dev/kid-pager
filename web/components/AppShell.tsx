"use client";

/** The nav shell for signed-in pages (applied once, by the router). What it contains comes from
 * `components/shell/sections.ts`:
 * - md and up: one app bar with plain links (Messages, Alerts, Family, System) and the account
 *   menu; a section's sub-pages are a tab row under it.
 * - below md: a slim top bar and a fixed bottom navigation (hidden on chat threads). Threads and
 *   `/location` get a back arrow to Messages in the top bar.
 * The phone/desktop split is CSS (`display`), never JS, so the static export cannot flash the
 * wrong layout. */

import type { ReactNode } from "react";
import AppBar from "@mui/material/AppBar";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import AdminPanelSettingsOutlinedIcon from "@mui/icons-material/AdminPanelSettingsOutlined";
import Badge from "@mui/material/Badge";
import BottomNavigation from "@mui/material/BottomNavigation";
import BottomNavigationAction from "@mui/material/BottomNavigationAction";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import ForumOutlinedIcon from "@mui/icons-material/ForumOutlined";
import GroupsOutlinedIcon from "@mui/icons-material/GroupsOutlined";
import HomeOutlinedIcon from "@mui/icons-material/HomeOutlined";
import IconButton from "@mui/material/IconButton";
import NotificationsActiveOutlinedIcon from "@mui/icons-material/NotificationsActiveOutlined";
import Paper from "@mui/material/Paper";
import SettingsRemoteOutlinedIcon from "@mui/icons-material/SettingsRemoteOutlined";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";

import { useAuth } from "@/lib/auth-context";
import { CHROME } from "@/lib/layout";
import { Link, usePathname } from "@/lib/router";

import NotificationWatcher from "./NotificationWatcher";
import Logo from "./Logo";
import AccountMenu from "./shell/AccountMenu";
import FamilySwitcher from "./shell/FamilySwitcher";
import { ShellDataProvider, useOpenAlertCount } from "./shell/ShellData";
import {
  type Viewer,
  backTarget,
  bottomNavFor,
  isBottomItemActive,
  isThreadPath,
  sectionOf,
  visibleSections,
} from "./shell/sections";

export { useOpenAlertCount } from "./shell/ShellData";

const BOTTOM_ICONS: Record<string, ReactNode> = {
  home: <HomeOutlinedIcon />,
  messages: <ForumOutlinedIcon />,
  alerts: <NotificationsActiveOutlinedIcon />,
  family: <GroupsOutlinedIcon />,
  system: <AdminPanelSettingsOutlinedIcon />,
  pager: <SettingsRemoteOutlinedIcon />,
};

const SAFE_BOTTOM = "env(safe-area-inset-bottom)";

function Brand() {
  return (
    <Box
      component={Link}
      href="/"
      sx={{ display: "flex", alignItems: "center", gap: 1, color: "inherit", textDecoration: "none" }}
    >
      <Logo size={28} />
      <Typography variant="h6" component="span">
        Pager
      </Typography>
    </Box>
  );
}

function AlertBadge({ children, offset = -10 }: { children: ReactNode; offset?: number }) {
  const count = useOpenAlertCount();
  return (
    <Badge color="error" badgeContent={count} sx={{ "& .MuiBadge-badge": { right: offset, top: -2 } }}>
      {children}
    </Badge>
  );
}

function DesktopBar({ viewer, activeId }: { viewer: Viewer; activeId: string | null }) {
  const { isSuper } = useAuth();
  return (
    <Toolbar sx={{ gap: 1, display: { xs: "none", md: "flex" }, minHeight: { md: CHROME.appBar.md } }}>
      <Box sx={{ mr: 2 }}>
        <Brand />
      </Box>
      {visibleSections(viewer).map((s) => {
        const active = s.id === activeId;
        return (
          <Button
            key={s.id}
            color="inherit"
            component={Link}
            href={s.href}
            aria-current={active ? "page" : undefined}
            sx={{
              alignSelf: "stretch",
              borderRadius: 0,
              px: 2,
              fontWeight: active ? 700 : 400,
              borderBottom: "3px solid",
              borderBottomColor: active ? "currentColor" : "transparent",
            }}
          >
            {s.id === "alerts" ? <AlertBadge>{s.label}</AlertBadge> : s.label}
          </Button>
        );
      })}
      <Box sx={{ flexGrow: 1 }} />
      {isSuper && <FamilySwitcher />}
      <AccountMenu />
    </Toolbar>
  );
}

function PhoneBar({ back }: { back: { href: string; label: string } | null }) {
  const { isSuper } = useAuth();
  return (
    <Toolbar sx={{ gap: 1, display: { xs: "flex", md: "none" }, minHeight: { xs: CHROME.appBar.xs } }}>
      {back && (
        <IconButton color="inherit" edge="start" component={Link} href={back.href} aria-label={back.label}>
          <ArrowBackIcon />
        </IconButton>
      )}
      <Brand />
      <Box sx={{ flexGrow: 1 }} />
      {isSuper && <FamilySwitcher />}
      <AccountMenu />
    </Toolbar>
  );
}

function SectionTabs({ viewer, pathname }: { viewer: Viewer; pathname: string }) {
  const { tabs, activeTab } = sectionOf(pathname, viewer);
  if (tabs.length === 0) return null;
  return (
    <Box sx={{ bgcolor: "background.paper", borderBottom: 1, borderColor: "divider" }}>
      <Container maxWidth="md" disableGutters sx={{ px: { xs: 0, md: 1.5 } }}>
        <Tabs
          value={activeTab?.href ?? false}
          variant="scrollable"
          scrollButtons="auto"
          aria-label="Section pages"
          sx={{ minHeight: CHROME.tabRow }}
        >
          {tabs.map((t) => (
            <Tab
              key={t.href}
              value={t.href}
              label={t.label}
              component={Link}
              href={t.href}
              sx={{ minHeight: CHROME.tabRow, minWidth: 0, px: { xs: 1.25, sm: 2 } }}
            />
          ))}
        </Tabs>
      </Container>
    </Box>
  );
}

function BottomBar({ viewer, pathname }: { viewer: Viewer; pathname: string }) {
  const items = bottomNavFor(viewer);
  const active = items.find((i) => isBottomItemActive(i.id, pathname, viewer))?.id ?? false;
  return (
    <Paper
      component="nav"
      aria-label="Main"
      elevation={3}
      sx={{
        display: { xs: "block", md: "none" },
        position: "fixed",
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: (t) => t.zIndex.appBar,
        borderRadius: 0,
        pb: SAFE_BOTTOM,
      }}
    >
      <BottomNavigation showLabels value={active} sx={{ height: CHROME.bottomNav }}>
        {items.map((i) => (
          <BottomNavigationAction
            key={i.id}
            value={i.id}
            label={i.label}
            component={Link}
            href={i.href}
            aria-current={i.id === active ? "page" : undefined}
            icon={i.id === "alerts" ? <AlertBadge offset={-4}>{BOTTOM_ICONS.alerts}</AlertBadge> : BOTTOM_ICONS[i.id]}
            sx={{ minWidth: 0, px: 0.5 }}
          />
        ))}
      </BottomNavigation>
    </Paper>
  );
}

function Frame({ children }: { children: ReactNode }) {
  const { isFamilyAdmin, isSuper } = useAuth();
  const pathname = usePathname() ?? "";
  const viewer: Viewer = { isFamilyAdmin, isSuper };
  const thread = isThreadPath(pathname);
  const activeId = sectionOf(pathname, viewer).section?.id ?? null;
  // Phones: room for the fixed bottom bar (not on threads: the composer owns the bottom edge).
  const bottomPad = thread
    ? `calc(${CHROME.padY.xs}px + ${SAFE_BOTTOM})`
    : `calc(${CHROME.bottomNav + CHROME.padY.xs}px + ${SAFE_BOTTOM})`;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <NotificationWatcher />
      <AppBar position="static" color="primary" enableColorOnDark elevation={0}>
        <DesktopBar viewer={viewer} activeId={activeId} />
        <PhoneBar back={backTarget(pathname)} />
      </AppBar>
      <SectionTabs viewer={viewer} pathname={pathname} />
      <Container
        maxWidth="md"
        sx={{
          flexGrow: 1,
          px: { xs: 1.5, md: 3 },
          pt: { xs: `${CHROME.padY.xs}px`, md: `${CHROME.padY.md}px` },
          pb: { xs: bottomPad, md: `${CHROME.padY.md}px` },
        }}
      >
        {children}
      </Container>
      {!thread && <BottomBar viewer={viewer} pathname={pathname} />}
    </Box>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <ShellDataProvider>
      <Frame>{children}</Frame>
    </ShellDataProvider>
  );
}
