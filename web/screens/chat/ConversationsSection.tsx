"use client";

/** Messages dashboard, "Conversations": the viewer's own conversations (last message, unread badge,
 * leave a group) with the New chat / New group actions. Loads on its own. */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Badge from "@mui/material/Badge";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import GroupsIcon from "@mui/icons-material/Groups";
import LogoutIcon from "@mui/icons-material/Logout";
import PhoneIcon from "@mui/icons-material/Phone";

import NewChatDialog from "@/components/NewChatDialog";
import NewGroupDialog from "@/components/NewGroupDialog";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { myConversationsQuery } from "@/lib/live/queries";
import { useNavigate } from "@/lib/router";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc } from "@/lib/types";

import { type ConversationRow, conversationRows } from "./messagesModel";

export default function ConversationsSection() {
  const { me, isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  const { byUid } = useDirectory();
  const navigate = useNavigate();
  const [newChatOpen, setNewChatOpen] = useState(false);
  const [newGroupOpen, setNewGroupOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // the queries need `familyId` for the rules' `sameFam` clause
  const live = useLive<ConversationDoc>(me && familyId ? myConversationsQuery(me.uid) : null);
  const rows = useMemo(
    () => (me ? conversationRows(live.docs, me.uid, byUid) : []),
    [live.docs, me, byUid]
  );

  // `DELETE /api/conversations/{alias}/members/me`
  async function leaveGroup(c: ConversationRow) {
    if (!c.groupAlias) return;
    if (!window.confirm(`Leave "${c.label}"? You'll keep read access to messages you already received.`)) {
      return;
    }
    setActionError(null);
    try {
      await api.del(`/conversations/${encodeURIComponent(c.groupAlias)}/members/me`);
    } catch (err) {
      setActionError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to leave group");
    }
  }

  return (
    <Box component="section" aria-labelledby="messages-conversations" sx={{ minWidth: 0 }}>
      <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap", mb: 1 }}>
        <Typography variant="h6" component="h2" id="messages-conversations" sx={{ flexGrow: 1 }}>
          Conversations
        </Typography>
        <Button variant="contained" size="small" onClick={() => setNewChatOpen(true)}>
          New chat
        </Button>
        {isFamilyAdmin && (
          <Button variant="outlined" size="small" onClick={() => setNewGroupOpen(true)} sx={{ whiteSpace: "nowrap" }}>
            New group
          </Button>
        )}
      </Stack>

      {actionError && (
        <Alert severity="error" sx={{ mb: 1 }}>
          {actionError}
        </Alert>
      )}

      {live.error ? (
        <Alert severity="error">Couldn&apos;t load your conversations. Try reloading the page.</Alert>
      ) : live.loading ? (
        <Skeleton variant="rounded" height={160} />
      ) : rows.length === 0 ? (
        <Card variant="outlined" sx={{ p: 2 }}>
          <Typography variant="body2" color="text.secondary">
            No conversations yet. Use New chat to start one.
          </Typography>
        </Card>
      ) : (
        <Card variant="outlined">
          <List disablePadding>
            {rows.map((c, i) => (
              <ListItem
                key={c.convKey}
                disablePadding
                divider={i < rows.length - 1}
                secondaryAction={
                  c.isGroup && !c.bridge ? (
                    <IconButton edge="end" aria-label={`leave ${c.label}`} onClick={() => void leaveGroup(c)}>
                      <LogoutIcon fontSize="small" />
                    </IconButton>
                  ) : undefined
                }
              >
                <ListItemButton onClick={() => navigate.push(`/chat/${encodeURIComponent(c.routeAlias)}`)}>
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
                        {c.isGroup && <GroupsIcon fontSize="small" color="action" />}
                        {c.phone && <PhoneIcon fontSize="small" color="action" />}
                        <Typography sx={{ fontWeight: c.unread > 0 ? 700 : 400 }}>{c.label}</Typography>
                        {c.unread > 0 && <Badge color="primary" badgeContent={c.unread} />}
                      </Stack>
                    }
                    secondary={
                      <>
                        {c.phone && (
                          <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>
                            +{c.phone}
                          </Typography>
                        )}
                        {c.lastPreview || "(no messages yet)"}
                      </>
                    }
                  />
                  {tsToMillis(c.lastMessageAt) !== null && (
                    <Typography variant="caption" color="text.secondary" sx={{ mr: c.isGroup && !c.bridge ? 4 : 0 }}>
                      {formatRelativeAge(tsToMillis(c.lastMessageAt)!)}
                    </Typography>
                  )}
                </ListItemButton>
              </ListItem>
            ))}
          </List>
        </Card>
      )}

      <NewChatDialog open={newChatOpen} onClose={() => setNewChatOpen(false)} />
      <NewGroupDialog open={newGroupOpen} onClose={() => setNewGroupOpen(false)} />
    </Box>
  );
}
