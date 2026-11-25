"use client";

/** "Defaults" and "Also affects who can talk" on `/family/permissions`: what a new person starts
 * with (there is no family-wide default to edit), and the controls that live on other pages. */

import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Typography from "@mui/material/Typography";

import SectionBlock from "@/components/SectionBlock";
import { useAuth } from "@/lib/auth-context";
import { Link } from "@/lib/router";
import { newPersonLine } from "./permissionsModel";

export function DefaultsSection() {
  return (
    <SectionBlock
      id="defaults"
      title="Defaults"
      description="What a person gets when you add them, before you change anything."
    >
      <Typography variant="body2">{newPersonLine()}</Typography>
    </SectionBlock>
  );
}

export function AlsoAffectsSection() {
  const { isSuper } = useAuth();
  const rows = [
    {
      href: "/family/people#contacts",
      title: "Outside contacts",
      line: "Phone numbers your family texts, and which people each is approved for.",
    },
    {
      href: "/family/people#federation",
      title: "Linked families",
      line: "Share members with other families and choose who they can reach.",
    },
    { href: "/alerts", title: "Alerts", line: "Requests waiting for your OK, such as a pager asking to text someone new." },
    ...(isSuper
      ? [
          {
            href: "/admin/allowlist",
            title: "Allow-list",
            line: "Super only: every pair of users across all families, with Message and Locate switches.",
          },
        ]
      : []),
  ];
  return (
    <SectionBlock
      id="also"
      title="Also affects who can talk"
      description="Other places that decide who can reach whom."
    >
      <List disablePadding sx={{ border: 1, borderColor: "divider", borderRadius: 1 }}>
        {rows.map((r) => (
          <ListItemButton key={r.href} component={Link} href={r.href} divider>
            <ListItemText primary={r.title} secondary={r.line} />
          </ListItemButton>
        ))}
      </List>
    </SectionBlock>
  );
}
