"use client";

import { useState } from "react";
import Avatar from "@mui/material/Avatar";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Typography from "@mui/material/Typography";

import { useAuth } from "@/lib/auth-context";
import { Link } from "@/lib/router";

/** "Ada Lovelace" -> "AL"; one word -> its first letter. */
export function initialsOf(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  const first = words[0]![0]!;
  const last = words.length > 1 ? words[words.length - 1]![0]! : "";
  return (first + last).toUpperCase();
}

/** Avatar button + the account menu (the only menu in the shell). */
export default function AccountMenu() {
  const { me, isFamilyAdmin, signOutUser } = useAuth();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const close = () => setAnchor(null);
  if (!me) return null;

  return (
    <>
      <IconButton
        color="inherit"
        aria-label="Account menu"
        aria-haspopup="menu"
        onClick={(e) => setAnchor(e.currentTarget)}
        sx={{ p: 0.5 }}
      >
        <Avatar sx={{ width: 32, height: 32, fontSize: 14, bgcolor: "rgba(255,255,255,0.22)", color: "inherit" }}>
          {initialsOf(me.displayName)}
        </Avatar>
      </IconButton>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close}>
        <li role="presentation" style={{ listStyle: "none", padding: "6px 16px 8px" }}>
          <Typography variant="subtitle1" sx={{ wordBreak: "break-word", lineHeight: 1.3 }}>
            {me.displayName}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            @{me.alias}
          </Typography>
        </li>
        <Divider />
        <MenuItem component={Link} href="/account/notifications" onClick={close}>
          Notifications
        </MenuItem>
        <MenuItem component={Link} href="/account/apps" onClick={close}>
          Connected apps
        </MenuItem>
        <MenuItem component={Link} href="/account/pagers" onClick={close}>
          My pagers
        </MenuItem>
        {!isFamilyAdmin && (
          <MenuItem component={Link} href="/settings/book" onClick={close}>
            Address book
          </MenuItem>
        )}
        <Divider />
        <MenuItem
          onClick={() => {
            close();
            void signOutUser();
          }}
        >
          Sign out
        </MenuItem>
      </Menu>
    </>
  );
}
