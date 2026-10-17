"use client";

/** `/devices/[id]`: battery card, SMS contacts and audit log (`/api/devices/{id}/sms-contacts`,
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
import EditDeviceLabelButton from "@/components/EditDeviceLabelDialog";
import FirmwareChip from "@/components/FirmwareChip";
import FirmwareUpdateDialog from "@/components/FirmwareUpdateDialog";
import FontsChip from "@/components/FontsChip";
import FontsUpdateDialog from "@/components/FontsUpdateDialog";
import { isAssetJob } from "@/lib/assets";
import RequireAuth from "@/components/RequireAuth";
import BatteryCard from "@/components/BatteryCard";
import GnssPanel from "@/components/GnssPanel";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { deviceName } from "@/lib/devices";
import { useNewestBuild } from "@/lib/firmware";
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
import { responsiveTableSx } from "@/lib/tableSx";

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

function LogRow({ e }: { e: SmsLogEntry }) {
  const [showRaw, setShowRaw] = useState(false);
  const bad = e.malformed === true;
  return (
    <TableRow
      sx={{
        bgcolor: bad
          ? "warning.light"
          : e.st === "blocked"
            ? "warning.light"
            : e.st === "failed"
              ? "error.light"
              : undefined,
      }}
    >
      <TableCell>{formatLogTs(e.smsTs || e.ts)}</TableCell>
      <TableCell>{e.dir ? dirArrow(e.dir) : "—"}</TableCell>
      <TableCell>{e.name ?? e.peer ?? "—"}</TableCell>
      <TableCell>
        {bad && <Chip size="small" label="malformed" color="error" sx={{ mr: 0.5 }} />}
        {e.st ? <Chip size="small" label={e.st} color={statusColor(e.st)} /> : !bad && "—"}
      </TableCell>
      <TableCell sx={{ maxWidth: 240, wordBreak: "break-word" }}>
        {bad ? (
          <>
            <Typography component="span" variant="body2" sx={{ fontStyle: "italic" }}>
              {e.reason ?? "—"}
            </Typography>
            {e.rawHex && (
              <>
                {" "}
                <Button size="small" sx={{ minWidth: 0, p: 0, textTransform: "none" }} onClick={() => setShowRaw((v) => !v)}>
                  raw
                </Button>
                {showRaw && (
                  <Typography
                    component="div"
                    variant="caption"
                    sx={{ fontFamily: "monospace", wordBreak: "break-all" }}
                  >
                    {e.rawHex}
                  </Typography>
                )}
              </>
            )}
          </>
        ) : (
          (e.body ?? "—")
        )}
      </TableCell>
    </TableRow>
  );
}

function DeviceInner() {
  const id = useDeviceId();
  const router = useRouter();
  const { isSuper, isFamilyAdmin } = useAuth();
  // A super uses the account-level admin routes; a family admin the family ones.
  const fwScope = isSuper ? "admin" : "family";
  const newestBuild = useNewestBuild(isFamilyAdmin, fwScope, id);
  const [fwOpen, setFwOpen] = useState(false);
  const [fontsOpen, setFontsOpen] = useState(false);

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
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
        <IconButton onClick={() => router.back()} aria-label="back" size="small">
          <ArrowBackIcon fontSize="small" />
        </IconButton>
        <Stack sx={{ flexGrow: 1 }}>
          <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
            <Typography variant="h5">{device ? deviceName(device) : id}</Typography>
            {device && isFamilyAdmin && <EditDeviceLabelButton device={device} scope={fwScope} />}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
            {id}
          </Typography>
        </Stack>
        <DeviceTrustChip tls={device?.status?.tls} caFp={device?.status?.caFp} />
        {device?.status?.car && <Chip size="small" variant="outlined" label={device.status.car} />}
      </Stack>

      {deviceError && <Alert severity="warning">{deviceError}</Alert>}

      {device && (
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "flex-start" }}>
          <FirmwareChip status={device.status} newest={newestBuild} assetJob={isAssetJob(device)} />
          {isFamilyAdmin && (
            <Button size="small" onClick={() => setFwOpen(true)}>
              Update firmware…
            </Button>
          )}
          <FontsChip device={device} scope={fwScope} />
          {isFamilyAdmin && (
            <Button size="small" onClick={() => setFontsOpen(true)}>
              Update fonts…
            </Button>
          )}
        </Stack>
      )}
      {fontsOpen && <FontsUpdateDialog device={device} scope={fwScope} open onClose={() => setFontsOpen(false)} />}
      {fwOpen && <FirmwareUpdateDialog device={device} scope={fwScope} open onClose={() => setFwOpen(false)} />}

      {smsLost > 0 && (
        <Alert severity="warning">
          {smsLost} SMS log {smsLost === 1 ? "entry was" : "entries were"} lost on the pager before
          they could be uploaded.
        </Alert>
      )}

      {id && id !== "_" && <GnssPanel deviceId={id} />}

      {id && id !== "_" && <BatteryCard deviceId={id} />}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" gutterBottom>
            SMS contacts
          </Typography>
          {device?.status?.tls === "proxy" && (
            <Typography variant="body2" sx={{ mb: 1 }}>
              Device SMS unavailable on this carrier; relay SMS still works
            </Typography>
          )}
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
            <TableContainer sx={responsiveTableSx([])}>
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
          <Stack direction="row" sx={{ flexWrap: "wrap", alignItems: "center", mb: 1 }}>
            <Typography variant="h6" sx={{ flexGrow: 1 }}>
              SMS log
            </Typography>
            <Button size="small" onClick={() => void fetchLog(undefined)}>
              Refresh
            </Button>
          </Stack>
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
            <TableContainer sx={responsiveTableSx([2,4])}>
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
                    <LogRow key={e.id} e={e} />
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
