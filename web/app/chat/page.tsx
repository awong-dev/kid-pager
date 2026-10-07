"use client";

/** `/chat`: conversation list (last message, unread badge) and device status, all live reads. */

import { useRouter } from "next/navigation";
import Alert from "@mui/material/Alert";
import Badge from "@mui/material/Badge";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Typography from "@mui/material/Typography";
import BatteryStdIcon from "@mui/icons-material/BatteryStd";
import GroupsIcon from "@mui/icons-material/Groups";
import LogoutIcon from "@mui/icons-material/Logout";
import PhoneIcon from "@mui/icons-material/Phone";
import VisibilityIcon from "@mui/icons-material/Visibility";

import AppShell from "@/components/AppShell";
import NewChatDialog from "@/components/NewChatDialog";
import NewGroupDialog from "@/components/NewGroupDialog";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc, DeviceDoc } from "@/lib/types";

interface ConversationRow extends ConversationDoc {
  convKey: string;
  // `null` for a group row
  peerUid: string | null;
}

// A "Family" tab row: a conversation this admin is not a party to, named from `participants`.
interface FamilyRow {
  convKey: string;
  label: string;
  /** E.164 digits of an SMS-external party, shown under the label. */
  phone?: string;
  lastPreview: string;
  lastMessageAt: ConversationDoc["lastMessageAt"];
}

/** An external's number as digits (no `+`); its alias is a hash, so only `phone` carries it. */
function externalDigits(phone: string | null | undefined): string | undefined {
  return phone ? phone.replace(/^\+/, "") : undefined;
}

function ChatListInner() {
  const { me, isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  const { byUid } = useDirectory();
  const router = useRouter();
  const [newChatOpen, setNewChatOpen] = useState(false);
  const [newGroupOpen, setNewGroupOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // the "Family" tab is admin only
  const [tab, setTab] = useState<"mine" | "family">("mine");



  // Family tab: conversations of this family that I am not a party to; the rules' family-admin
  // clause makes the query legal for admins only.

  // Name DM peers from the conversation's `participants`, falling back to the directory; a DM
  // with neither is dropped. A group routes by its own `alias`.
  const peerLabels = useMemo(
    () =>
      conversations.flatMap((c) => {
        if (c.kind === "group") {
          const label = c.name ?? c.alias ?? c.convKey;
          return [{ ...c, label, routeAlias: c.alias ?? c.convKey, phone: undefined as string | undefined }];
        }
        const peerUid = c.peerUid;
        const participant = peerUid ? c.participants?.[peerUid] : undefined;
        const directoryEntry = peerUid ? byUid(peerUid) : undefined;
        const alias = participant?.alias ?? directoryEntry?.alias;
        if (!alias) return [];
        const kind = participant?.kind ?? directoryEntry?.kind;
        const phone = kind === "external" ? externalDigits(participant?.phone ?? directoryEntry?.phone) : undefined;
      }),
    [conversations, byUid]
  );

  // "@kid <-> @peer" (or the group name); `byUid` only tells which uid is in my family.
  const familyRows = useMemo<FamilyRow[]>(
    () =>
      familyConversations.map((c) => {
        if (c.kind === "group") {
          return {
            convKey: c.convKey,
            label: c.name ?? c.alias ?? c.convKey,
            lastPreview: c.lastPreview,
            lastMessageAt: c.lastMessageAt,
          };
        }
        const participants = c.participants ?? {};
        const kidUid = c.uids.find((u) => byUid(u)?.familyId === familyId) ?? c.uids[0];
        const peerUid = c.uids.find((u) => u !== kidUid);
        const kidAlias = (kidUid && (participants[kidUid]?.alias ?? byUid(kidUid)?.alias)) ?? "?";
        const peerEntry = peerUid ? participants[peerUid] : undefined;
        return {
          convKey: c.convKey,
          phone:
            peerEntry?.kind === "external"
              ? externalDigits(peerEntry.phone ?? (peerUid ? byUid(peerUid)?.phone : undefined))
              : undefined,
          lastPreview: c.lastPreview,
          lastMessageAt: c.lastMessageAt,
        };
      }),
    [familyConversations, byUid, familyId]
  );

  function openConversation(alias: string) {
    const trimmed = alias.trim();
    if (!trimmed) return;
    router.push(`/chat/${encodeURIComponent(trimmed)}`);
  }

  function openMonitor(convKey: string) {
    router.push(`/chat/view/${encodeURIComponent(convKey)}`);
  }

  // `DELETE /api/conversations/{alias}/members/me`
  async function leaveGroup(c: { alias?: string; label: string }) {
    if (!c.alias) return;
    if (!window.confirm(`Leave "${c.label}"? You'll keep read access to messages you already received.`)) {
      return;
    }
    setActionError(null);
    try {
      await api.del(`/conversations/${encodeURIComponent(c.alias)}/members/me`);
    } catch (err) {
      setActionError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to leave group");
    }
  }

  return (
    <Stack spacing={3}>
      {device && (
        <Card variant="outlined">
          <CardContent>
              {device.status.state === "online" ? (
              ) : (
              )}
              <Typography variant="body1">
              </Typography>
              {device.status.battMv != null && (
                <Chip
                  icon={<BatteryStdIcon />}
                  size="small"
                />
              )}
              {tsToMillis(device.status.updatedAt) !== null && (
                <Typography variant="caption" color="text.secondary">
                  updated {formatRelativeAge(tsToMillis(device.status.updatedAt)!)}
                </Typography>
              )}
            </Stack>
          </CardContent>
        </Card>
      )}

      {isFamilyAdmin && (
        <Tabs value={tab} onChange={(_, v: "mine" | "family") => setTab(v)}>
          <Tab value="mine" label="My chats" />
          <Tab value="family" label="Family" />
        </Tabs>
      )}

      {tab === "mine" ? (
        <>
            <Button variant="contained" onClick={() => setNewChatOpen(true)}>
              New chat
            </Button>
            {isFamilyAdmin && (
              <Button variant="outlined" onClick={() => setNewGroupOpen(true)} sx={{ whiteSpace: "nowrap" }}>
                New group
              </Button>
            )}
          </Stack>

          {actionError && <Alert severity="error">{actionError}</Alert>}

          <Divider />

          {peerLabels.length === 0 && (
            <Alert severity="info">
              No conversations yet. Start one with New chat above -- ask your admin who you&apos;re
              allowed to message.
            </Alert>
          )}

          <List disablePadding>
            {peerLabels.map((c) => {
              const unread = c.unread?.[me?.uid ?? ""] ?? 0;
              const isGroup = c.kind === "group";
              return (
                <ListItem
                  key={c.convKey}
                  disablePadding
                  divider
                  secondaryAction={
                      <IconButton edge="end" aria-label={`leave ${c.label}`} onClick={() => void leaveGroup(c)}>
                        <LogoutIcon fontSize="small" />
                      </IconButton>
                    ) : undefined
                  }
                >
                  <ListItemButton onClick={() => openConversation(c.routeAlias)}>
                    <ListItemText
                      primary={
                          {isGroup && <GroupsIcon fontSize="small" color="action" />}
                          {c.phone && <PhoneIcon fontSize="small" color="action" />}
                          <Typography sx={{ fontWeight: unread > 0 ? 700 : 400 }}>
                          </Typography>
                          {unread > 0 && <Badge color="primary" badgeContent={unread} />}
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
                      <Typography variant="caption" color="text.secondary" sx={{ mr: isGroup ? 4 : 0 }}>
                        {formatRelativeAge(tsToMillis(c.lastMessageAt)!)}
                      </Typography>
                    )}
                  </ListItemButton>
                </ListItem>
              );
            })}
          </List>
        </>
      ) : (
        <>
          {familyRows.length === 0 && (
            <Alert severity="info">No conversations from your family with anyone outside it yet.</Alert>
          )}
          <List disablePadding>
            {familyRows.map((r) => (
              <ListItem key={r.convKey} disablePadding divider>
                <ListItemButton onClick={() => openMonitor(r.convKey)}>
                  <VisibilityIcon fontSize="small" color="action" sx={{ mr: 1.5 }} />
                  <ListItemText
                    primary={
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
        </>
      )}

      <Box sx={{ minHeight: 0 }} />

      <NewChatDialog open={newChatOpen} onClose={() => setNewChatOpen(false)} />
      <NewGroupDialog open={newGroupOpen} onClose={() => setNewGroupOpen(false)} />
    </Stack>
  );
}

export default function ChatListPage() {
  return (
    <RequireAuth>
      <AppShell>
        <ChatListInner />
      </AppShell>
    </RequireAuth>
  );
}
