"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import AppBar from "@mui/material/AppBar";
import Badge from "@mui/material/Badge";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Divider from "@mui/material/Divider";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import ListSubheader from "@mui/material/ListSubheader";
import MenuIcon from "@mui/icons-material/Menu";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import AdminPanelSettingsIcon from "@mui/icons-material/AdminPanelSettings";
import ChatIcon from "@mui/icons-material/Chat";
import FamilyRestroomIcon from "@mui/icons-material/FamilyRestroom";
import LocationOnIcon from "@mui/icons-material/LocationOn";
import SettingsIcon from "@mui/icons-material/Settings";

import { useAuth } from "@/lib/auth-context";
import { useFamily } from "@/lib/family-context";
import { useLocatableDevices } from "@/lib/locatableDevices";

import NotificationWatcher from "./NotificationWatcher";

const settingsLinks = [
  { href: "/settings/book", label: "Address book" },
  { href: "/settings/backends", label: "Backends" },
  { href: "/settings/notifications", label: "Notifications" },
  { href: "/settings/devices", label: "My devices" },
];

// Family admin menu, backed by `/api/family/*`.
const familyLinks = [
  { href: "/family/people", label: "People" },
  { href: "/family/devices", label: "Devices" },
  { href: "/family/contacts", label: "Contacts" },
  { href: "/family/chat", label: "Bridged chats" },
  { href: "/family/alerts", label: "Alerts" },
];

// Superadmin menu.
const adminLinks = [
  { href: "/admin/families", label: "Families" },
  { href: "/admin/users", label: "All users" },
  { href: "/admin/devices", label: "All devices" },
  { href: "/admin/allowlist", label: "Allow-list" },
  { href: "/admin/settings", label: "Retention" },
];

function NavMenu({
  label,
  icon,
  links,
  badgeCount,
}: {
  label: string;
  icon: ReactNode;
  links: { href: string; label: string }[];
  /** Open-alert count badge on the menu button and the `Alerts` item; 0 or undefined shows none. */
  badgeCount?: number;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const pathname = usePathname();
  const isActive = links.some((l) => pathname?.startsWith(l.href));

  return (
    <>
      <Button
        color="inherit"
        startIcon={icon}
        onClick={(e) => setAnchor(e.currentTarget)}
        sx={{ fontWeight: isActive ? 700 : 400 }}
      >
        <Badge color="error" badgeContent={badgeCount} sx={{ "& .MuiBadge-badge": { right: -8, top: -2 } }}>
          {label}
        </Badge>
      </Button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {links.map((l) => (
          <MenuItem key={l.href} component={Link} href={l.href} onClick={() => setAnchor(null)}>
            {l.href === "/family/alerts" ? (
              <Badge
                color="error"
                badgeContent={badgeCount}
                sx={{ "& .MuiBadge-badge": { right: -12 } }}
              >
                {l.label}
              </Badge>
            ) : (
              l.label
            )}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

/** The "Family: <name>" chip in the AppBar (super only); `setFamilyId` scopes every `/family/*`
 * page and `/api/family/*` call. */
function FamilySwitcher() {
  const { family, families, setFamilyId } = useFamily();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  if (families.length === 0) {
    return null;
  }

  return (
    <>
      <Chip
        color="secondary"
        label={`Family: ${family?.name ?? "choose…"} ▾`}
        onClick={(e) => setAnchor(e.currentTarget)}
        sx={{ cursor: "pointer" }}
      />
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {families.map((f) => (
          <MenuItem
            key={f.id}
            selected={f.id === family?.id}
            onClick={() => {
              setFamilyId(f.id);
              setAnchor(null);
            }}
          >
            {f.name}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

/** Mobile nav drawer content: the AppBar links as a flat list; every link calls `onNavigate`. */
function MobileNavList({
  onNavigate,
  showLocation,
  openAlertCount,
}: {
  onNavigate: () => void;
  showLocation: boolean;
  openAlertCount: number;
}) {
  const { me, isFamilyAdmin, isSuper, signOutUser } = useAuth();
  const { family, families, setFamilyId } = useFamily();
  const pathname = usePathname();

  const section = (title: string, links: { href: string; label: string }[]) => (
    <List
      dense
      subheader={<ListSubheader sx={{ lineHeight: "32px" }}>{title}</ListSubheader>}
    >
      {links.map((l) => (
        <ListItemButton
          key={l.href}
          component={Link}
          href={l.href}
          selected={pathname?.startsWith(l.href)}
          onClick={onNavigate}
        >
          <ListItemText>
            {l.href === "/family/alerts" ? (
              <Badge color="error" badgeContent={openAlertCount} sx={{ "& .MuiBadge-badge": { right: -14 } }}>
                {l.label}
              </Badge>
            ) : (
              l.label
            )}
          </ListItemText>
        </ListItemButton>
      ))}
    </List>
  );

  return (
    <Box sx={{ width: 280, maxWidth: "85vw" }} role="navigation">
      {me && (
        <Box sx={{ px: 2, py: 1.5 }}>
          <Typography variant="subtitle1" sx={{ wordBreak: "break-word" }}>
            {me.displayName}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            @{me.alias}
          </Typography>
        </Box>
      )}
      <Divider />
      <List dense>
        <ListItemButton component={Link} href="/chat" selected={pathname?.startsWith("/chat")} onClick={onNavigate}>
          <ChatIcon fontSize="small" sx={{ mr: 1.5 }} />
          <ListItemText>Chat</ListItemText>
        </ListItemButton>
        {showLocation && (
          <ListItemButton component={Link} href="/location" selected={pathname?.startsWith("/location")} onClick={onNavigate}>
            <LocationOnIcon fontSize="small" sx={{ mr: 1.5 }} />
            <ListItemText>Location</ListItemText>
          </ListItemButton>
        )}
      </List>
      {isFamilyAdmin && section("Family", familyLinks)}
      {section("Settings", settingsLinks)}
      {isSuper && section("Admin", adminLinks)}
      {isSuper && families.length > 0 && (
        <List
          dense
          subheader={<ListSubheader sx={{ lineHeight: "32px" }}>Active family</ListSubheader>}
        >
          {families.map((f) => (
            <ListItemButton
              key={f.id}
              selected={f.id === family?.id}
              onClick={() => {
                setFamilyId(f.id);
                onNavigate();
              }}
            >
              <ListItemText>{f.name}</ListItemText>
            </ListItemButton>
          ))}
        </List>
      )}
      <Divider />
      <List dense>
        <ListItemButton
          onClick={() => {
            onNavigate();
            void signOutUser();
          }}
        >
          <ListItemText>Sign out</ListItemText>
        </ListItemButton>
      </List>
    </Box>
  );
}

/** Live count of open `families/{fam}/alerts` (rules: super or that family's admin). */
function useOpenAlertCount(familyId: string | null, enabled: boolean): number {
}

/** Top nav shell for signed-in pages. Members get Chat/Location/Settings; a family admin adds the
 * Family menu; super adds the Admin menu and the family switcher. */
export default function AppShell({ children }: { children: ReactNode }) {
  const { me, isFamilyAdmin, isSuper, signOutUser } = useAuth();
  const { familyId } = useFamily();
  const openAlertCount = useOpenAlertCount(familyId, isFamilyAdmin);
  // Nav visibility only, not the gate; a family admin always gets the link.
  const { devices: locatableDevices } = useLocatableDevices();
  const showLocation = isFamilyAdmin || locatableDevices.length > 0;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pathname = usePathname();
  // close the drawer on route change
  const [lastPath, setLastPath] = useState(pathname);
  if (lastPath !== pathname) {
    setLastPath(pathname);
    setDrawerOpen(false);
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <NotificationWatcher />
      <AppBar position="static" color="primary" enableColorOnDark>
        {/* below md: hamburger + title (CSS-toggled so the static export never flashes) */}
        <Toolbar sx={{ gap: 1, display: { xs: "flex", md: "none" } }}>
          <IconButton
            color="inherit"
            edge="start"
            aria-label="Open navigation"
            onClick={() => setDrawerOpen(true)}
          >
            <Badge color="error" badgeContent={openAlertCount}>
              <MenuIcon />
            </Badge>
          </IconButton>
          <Typography
            variant="h6"
            component={Link}
            href="/chat"
            sx={{ color: "inherit", textDecoration: "none" }}
          >
            Pager
          </Typography>
        </Toolbar>
        <Toolbar sx={{ gap: 1, display: { xs: "none", md: "flex" } }}>
          <Typography
            variant="h6"
            component={Link}
            href="/chat"
            sx={{ color: "inherit", textDecoration: "none", mr: 2 }}
          >
            Pager
          </Typography>
          <Button color="inherit" component={Link} href="/chat" startIcon={<ChatIcon />}>
            Chat
          </Button>
          {showLocation && (
            <Button color="inherit" component={Link} href="/location" startIcon={<LocationOnIcon />}>
              Location
            </Button>
          )}
          {isFamilyAdmin && (
            <NavMenu
              label="Family"
              icon={<FamilyRestroomIcon />}
              links={familyLinks}
              badgeCount={openAlertCount}
            />
          )}
          <NavMenu label="Settings" icon={<SettingsIcon />} links={settingsLinks} />
          {isSuper && (
            <NavMenu label="Admin" icon={<AdminPanelSettingsIcon />} links={adminLinks} />
          )}
          <Box sx={{ flexGrow: 1 }} />
          {isSuper && <FamilySwitcher />}
          {me && (
            <Typography variant="body2" sx={{ opacity: 0.85 }}>
              {me.displayName} (@{me.alias})
            </Typography>
          )}
          <Button color="inherit" onClick={() => void signOutUser()}>
            Sign out
          </Button>
        </Toolbar>
      </AppBar>
      <Drawer
        anchor="left"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sx={{ display: { xs: "block", md: "none" } }}
      >
        <MobileNavList
          onNavigate={() => setDrawerOpen(false)}
          showLocation={showLocation}
          openAlertCount={openAlertCount}
        />
      </Drawer>
      <Divider />
      <Container maxWidth="md" sx={{ flexGrow: 1, px: { xs: 1.5, md: 3 }, py: { xs: 2, md: 3 } }}>
        {children}
      </Container>
    </Box>
  );
}
