"use client";

/** Messages dashboard, "All family conversations" (family admin or super): every conversation of
 * the family the viewer is not in, read-only, each row opening `/chat/view/:key`. Long lists are
 * collapsed behind "Show all". Loads on its own; the query is legal for admins only (firestore.rules). */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import PhoneIcon from "@mui/icons-material/Phone";
import VisibilityIcon from "@mui/icons-material/Visibility";

import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { familyConversationsQuery } from "@/lib/live/queries";
import { useNavigate } from "@/lib/router";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc } from "@/lib/types";

import { AUDIT_COLLAPSE_OVER, auditRows, collapseAudit } from "./messagesModel";

export default function AuditSection() {
  const { me, isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  const { byUid } = useDirectory();
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);

  const live = useLive<ConversationDoc>(me && isFamilyAdmin && familyId ? familyConversationsQuery(familyId) : null);
  const rows = useMemo(
    () => (me ? auditRows(live.docs, me.uid, familyId, byUid) : []),
    [live.docs, me, familyId, byUid]
  );
  const { shown } = collapseAudit(rows, expanded);
  const canCollapse = rows.length > AUDIT_COLLAPSE_OVER;

  return (
    <Box component="section" aria-labelledby="messages-audit" sx={{ minWidth: 0 }}>
      <Typography variant="h6" component="h2" id="messages-audit">
        All family conversations
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Every conversation in your family, read-only.
      </Typography>

      {live.error ? (
        <Alert severity="error">Couldn&apos;t load the family conversations. Try reloading the page.</Alert>
      ) : live.loading ? (
        <Skeleton variant="rounded" height={120} />
      ) : rows.length === 0 ? (
        <Card variant="outlined" sx={{ p: 2 }}>
          <Typography variant="body2" color="text.secondary">
            No conversations from your family with anyone outside it yet.
          </Typography>
        </Card>
      ) : (
        <Card variant="outlined">
          <List disablePadding>
            {shown.map((r, i) => (
              <ListItem key={r.convKey} disablePadding divider={i < shown.length - 1 || canCollapse}>
                <ListItemButton onClick={() => navigate.push(`/chat/view/${encodeURIComponent(r.convKey)}`)}>
                  <VisibilityIcon fontSize="small" color="action" sx={{ mr: 1.5 }} />
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
                        {r.phone && <PhoneIcon fontSize="small" color="action" />}
                        <Typography>{r.label}</Typography>
                      </Stack>
                    }
                    secondary={
                      <>
                        {r.phone && (
                          <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>
                            +{r.phone}
                          </Typography>
                        )}
                        {r.lastPreview || "(no messages yet)"}
                      </>
                    }
                  />
                  {tsToMillis(r.lastMessageAt) !== null && (
                    <Typography variant="caption" color="text.secondary">
                      {formatRelativeAge(tsToMillis(r.lastMessageAt)!)}
                    </Typography>
                  )}
                </ListItemButton>
              </ListItem>
            ))}
          </List>
          {canCollapse && (
            <Box sx={{ p: 0.5 }}>
              <Button size="small" fullWidth onClick={() => setExpanded((e) => !e)} aria-expanded={expanded}>
                {expanded ? "Show fewer" : `Show all (${rows.length})`}
              </Button>
            </Box>
          )}
        </Card>
      )}
    </Box>
  );
}
