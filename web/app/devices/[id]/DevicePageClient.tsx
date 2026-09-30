"use client";

 * `.../sms-log?limit=100&before=<epoch s>`). The id comes from `usePathname()`. */

import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";

import AppShell from "@/components/AppShell";
import DeviceTrustChip from "@/components/DeviceTrustChip";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useDirectory } from "@/lib/directory";
import {
  type SmsContact,
  type SmsContactsResponse,
  type SmsLogEntry,
  type SmsLogResponse,
  dirArrow,
  statusColor,
} from "@/lib/smsContacts";
import type { DeviceDoc } from "@/lib/types";

const LOG_PAGE_SIZE = 100;

function useDeviceId(): string {
  const pathname = usePathname();
  return useMemo(() => {
    const segments = (pathname ?? "").split("/").filter(Boolean);
    return decodeURIComponent(segments[segments.length - 1] ?? "");
  }, [pathname]);
}

function formatLogTs(epochS: number): string {
  return new Date(epochS * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function DeviceInner() {
  const id = useDeviceId();
  const router = useRouter();

  const [contacts, setContacts] = useState<SmsContact[] | null>(null);
  const [pending, setPending] = useState(false);
  const [contactsError, setContactsError] = useState<string | null>(null);

  const [log, setLog] = useState<SmsLogEntry[] | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [logLoadingMore, setLogLoadingMore] = useState(false);
  const [logHasMore, setLogHasMore] = useState(true);


  // Both fetchers set state only after their `await`, so the mount effect may call them.
  const fetchContacts = useCallback(async () => {
    if (!id || id === "_") return;
    try {
      const resp = await api.get<SmsContactsResponse>(`/devices/${id}/sms-contacts`);
      setContacts(resp.contacts);
      setPending(resp.pending);
      setContactsError(null);
    } catch (err) {
      setContactsError(
        err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to load SMS contacts."
      );
    }
  }, [id]);

  const fetchLog = useCallback(
    async (before?: number) => {
      if (!id || id === "_") return;
      const qs = new URLSearchParams({ limit: String(LOG_PAGE_SIZE) });
      if (before !== undefined) qs.set("before", String(before));
      try {
        const resp = await api.get<SmsLogResponse>(`/devices/${id}/sms-log?${qs.toString()}`);
        setLog((prev) => (before === undefined ? resp.entries : [...(prev ?? []), ...resp.entries]));
        setLogHasMore(resp.entries.length >= LOG_PAGE_SIZE);
        setLogError(null);
      } catch (err) {
        setLogError(
          err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to load the SMS log."
        );
      }
    },
    [id]
  );

  // One-shot GET on mount; the lint flags any function reachable from an effect that can set state.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchContacts();
    void fetchLog(undefined);
  }, [fetchContacts, fetchLog]);

  const lastLogTs = log && log.length > 0 ? log[log.length - 1].ts : undefined;

  async function loadMore() {
    if (lastLogTs === undefined) return;
    setLogLoadingMore(true);
    try {
      await fetchLog(lastLogTs);
    } finally {
      setLogLoadingMore(false);
    }
  }

  const smsLost = device?.status?.smsLost ?? 0;
  const ownerAlias = useDirectory().byUid(device?.ownerUid ?? "")?.alias;

  return (
    <Stack spacing={2}>
        <IconButton onClick={() => router.back()} aria-label="back" size="small">
          <ArrowBackIcon fontSize="small" />
        </IconButton>
        <DeviceTrustChip tls={device?.status?.tls} caFp={device?.status?.caFp} />
      </Stack>

      {deviceError && <Alert severity="warning">{deviceError}</Alert>}

      {smsLost > 0 && (
        <Alert severity="warning">
          {smsLost} SMS log {smsLost === 1 ? "entry was" : "entries were"} lost on the pager before
          they could be uploaded.
        </Alert>
      )}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" gutterBottom>
            SMS contacts
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            The pager can text only these numbers, and only they can text it. Every text in either
            direction is recorded below. Managed from People &rarr; @{ownerAlias ?? "..."} &rarr;
            Approved numbers.
          </Typography>

          {pending && <Chip size="small" color="warning" label="waiting for the pager to confirm" sx={{ mb: 2 }} />}
          {contactsError && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {contactsError}
            </Alert>
          )}

          {contacts === null ? (
            <CircularProgress size={24} />
          ) : contacts.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No approved numbers yet.
            </Typography>
          ) : (
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Name</TableCell>
                    <TableCell>Phone</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {contacts.map((c, i) => (
                    <TableRow key={i}>
                      <TableCell>{c.name}</TableCell>
                      <TableCell>{c.phone}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          {logError && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {logError}
            </Alert>
          )}
          {log !== null && log.length === 0 && !logError && (
            <Typography variant="body2" color="text.secondary">
              No SMS activity yet.
            </Typography>
          )}
          {log !== null && log.length > 0 && (
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Time</TableCell>
                    <TableCell />
                    <TableCell>Who</TableCell>
                    <TableCell>Status</TableCell>
                    <TableCell>Text</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {log.map((e) => (
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          {log !== null && logHasMore && log.length > 0 && (
            <Box sx={{ mt: 2, textAlign: "center" }}>
              <Button
                size="small"
                onClick={() => void loadMore()}
                disabled={logLoadingMore}
              >
                Load more
              </Button>
            </Box>
          )}
          {log === null && <CircularProgress size={24} />}
        </CardContent>
      </Card>
    </Stack>
  );
}

export default function DevicePageClient() {
  return (
    <RequireAuth>
      <AppShell>
        <DeviceInner />
      </AppShell>
    </RequireAuth>
  );
}
