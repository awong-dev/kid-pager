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
import Typography from "@mui/material/Typography";
import BatteryStdIcon from "@mui/icons-material/BatteryStd";
import GroupsIcon from "@mui/icons-material/Groups";
import LogoutIcon from "@mui/icons-material/Logout";

import AppShell from "@/components/AppShell";
import NewGroupDialog from "@/components/NewGroupDialog";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc, DeviceDoc } from "@/lib/types";

interface ConversationRow extends ConversationDoc {
  convKey: string;
  // `null` for a group row
  peerUid: string | null;
}

function ChatListInner() {
  const router = useRouter();
  const [newGroupOpen, setNewGroupOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);



  const peerLabels = useMemo(
    () =>
        if (c.kind === "group") {
          const label = c.name ?? c.alias ?? c.convKey;
        }
        return {
        };
      }),
  );

  function openConversation(alias: string) {
    const trimmed = alias.trim();
    if (!trimmed) return;
    router.push(`/chat/${encodeURIComponent(trimmed)}`);
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





                  }

      <Box sx={{ minHeight: 0 }} />

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
