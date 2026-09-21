"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import Divider from "@mui/material/Divider";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import AdminPanelSettingsIcon from "@mui/icons-material/AdminPanelSettings";
import ChatIcon from "@mui/icons-material/Chat";
import SettingsIcon from "@mui/icons-material/Settings";

import { useAuth } from "@/lib/auth-context";

import NotificationWatcher from "./NotificationWatcher";

const settingsLinks = [
  { href: "/settings/backends", label: "Backends" },
  { href: "/settings/notifications", label: "Notifications" },
  { href: "/settings/devices", label: "My devices" },
];

const adminLinks = [
  { href: "/admin/allowlist", label: "Allow-list" },
  { href: "/admin/settings", label: "Retention" },
];

function NavMenu({
  label,
  icon,
  links,
}: {
  label: string;
  icon: ReactNode;
  links: { href: string; label: string }[];
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
      </Button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {links.map((l) => (
          <MenuItem key={l.href} component={Link} href={l.href} onClick={() => setAnchor(null)}>
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

/** Top nav shell for signed-in pages. Members get Chat/Location/Settings; a family admin adds the
export default function AppShell({ children }: { children: ReactNode }) {

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
          <NavMenu label="Settings" icon={<SettingsIcon />} links={settingsLinks} />
            <NavMenu label="Admin" icon={<AdminPanelSettingsIcon />} links={adminLinks} />
          )}
          <Box sx={{ flexGrow: 1 }} />
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
