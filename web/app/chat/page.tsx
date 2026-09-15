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
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import BatteryStdIcon from "@mui/icons-material/BatteryStd";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc, DeviceDoc } from "@/lib/types";

interface ConversationRow extends ConversationDoc {
  convKey: string;
}

function ChatListInner() {
  const router = useRouter();



  const peerLabels = useMemo(
    () =>
  );

  function openConversation(alias: string) {
    const trimmed = alias.trim();
    if (!trimmed) return;
    router.push(`/chat/${encodeURIComponent(trimmed)}`);
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





      <Box sx={{ minHeight: 0 }} />
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
