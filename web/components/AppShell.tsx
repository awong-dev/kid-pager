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

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <NotificationWatcher />
      <AppBar position="static" color="primary" enableColorOnDark>
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
      <Divider />
        {children}
      </Container>
    </Box>
  );
}
