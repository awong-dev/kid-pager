"use client";

/** The member list of `/family/permissions`: one row per person with their rules in words and how
 * many people and contacts are approved. A row is a link to `?who=<alias>`; the page owns the
 * selection (it lives in the URL). */

import Chip from "@mui/material/Chip";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Typography from "@mui/material/Typography";

import { Link } from "@/lib/router";
import { type ApprovedData, approvedLine, permissionsHref, rulesLine } from "./permissionsModel";
import type { MemberRow } from "./useFamilyMembers";

export default function PermissionsOverview({
  people,
  selectedUid,
  approved,
}: {
  people: readonly MemberRow[];
  selectedUid: string | null;
  approved: Readonly<Record<string, ApprovedData | undefined>>;
}) {
  if (people.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No one yet. Add people under People &amp; contacts.
      </Typography>
    );
  }
  return (
    <List disablePadding aria-label="Family members" sx={{ border: 1, borderColor: "divider", borderRadius: 1 }}>
      {people.map((m) => {
        const data = approved[m.alias];
        return (
          <ListItemButton
            key={m.uid}
            component={Link}
            href={permissionsHref(m.alias)}
            selected={m.uid === selectedUid}
            divider
            aria-current={m.uid === selectedUid ? "true" : undefined}
          >
            <ListItemText
              primary={
                <>
                  {m.displayName}{" "}
                  <Typography component="span" variant="body2" color="text.secondary">
                    @{m.alias}
                  </Typography>
                  {m.disabled && <Chip size="small" label="Disabled" sx={{ ml: 1 }} />}
                </>
              }
              secondary={
                <>
                  {rulesLine(m.policy)}
                  {data && (
                    <>
                      <br />
                      {approvedLine(data, m.policy.out)}
                    </>
                  )}
                </>
              }
            />
          </ListItemButton>
        );
      })}
    </List>
  );
}
