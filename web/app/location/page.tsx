"use client";

/** `/location`: the people the viewer can locate, with a timeline and map for the selected one
 * (access is enforced server-side). A `?who=<alias>` deep link is read once. */

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Skeleton from "@mui/material/Skeleton";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import LocationOnIcon from "@mui/icons-material/LocationOn";

import AppShell from "@/components/AppShell";
import LocationTimeline from "@/components/LocationTimeline";
import RequireAuth from "@/components/RequireAuth";
import { deviceName } from "@/lib/devices";
import { ApiError, api } from "@/lib/api";
import { locBackoffLabel } from "@/lib/deviceTrust";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { useLive } from "@/lib/live";
import { deviceLocationsQuery, retentionSettingsQuery } from "@/lib/live/queries";
import { buildLocationTimeline, locationsDaysOf, type LocationFixRow } from "@/lib/location";
import { useLocatableDevices, type LocatableDevice } from "@/lib/locatableDevices";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { LocationFixDoc, RetentionSettingsDoc } from "@/lib/types";

const MAP_HEIGHT_PX = 420;
const MOBILE_TIMELINE_HEIGHT_PX = 280;
// A limit instead of a `ts` range filter, which would need a composite index; the time window
// is applied client-side by `buildLocationTimeline`.
const LOCATIONS_QUERY_LIMIT = 4500; // 30 days (the retention ceiling) of 10-minute fixes

const LocationMap = dynamic(() => import("@/components/LocationMap"), {
  ssr: false,
  loading: () => <Skeleton variant="rectangular" height={MAP_HEIGHT_PX} sx={{ borderRadius: 1 }} />,
});

// no `window` during the static-export prerender
function readWhoParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("who");
}

function PersonListItem({
  device,
  alias,
  selected,
  onSelect,
}: {
  device: LocatableDevice;
  alias: string | undefined;
  selected: boolean;
  onSelect: () => void;
}) {
  const updatedMs = tsToMillis(device.status.updatedAt);
  return (
    <ListItemButton selected={selected} onClick={onSelect} sx={{ borderRadius: 1 }}>
      <ListItemText
        primary={device.isOwn ? "You" : alias ? `@${alias}` : `uid:${device.ownerUid.slice(0, 8)}`}
        secondary={`${deviceName(device)}${updatedMs !== null ? ` · updated ${formatRelativeAge(updatedMs)}` : ""}`}
      />
    </ListItemButton>
  );
}

/** The timeline and map for one selected device; the caller remounts it per device. */
function LocationDetail({
  device,
  ownerAlias,
  isMobile,
  onBack,
}: {
  device: LocatableDevice;
  ownerAlias: string | undefined;
  isMobile: boolean;
  onBack: () => void;
}) {
  const [manualFocusId, setManualFocusId] = useState<string | null>(null);
  const [locateBusy, setLocateBusy] = useState(false);
  const [snack, setSnack] = useState<string | null>(null);

  const { docs: fixDocs } = useLive<LocationFixDoc>(
    deviceLocationsQuery(device.id, LOCATIONS_QUERY_LIMIT)
  );
  const { docs: retentionDocs } = useLive<RetentionSettingsDoc>(retentionSettingsQuery());
  const windowDays = locationsDaysOf(retentionDocs[0]?.data);
  const fixRows = useMemo<LocationFixRow[]>(() => fixDocs.map((d) => ({ id: d.id, ...d.data })), [fixDocs]);

  const entries = useMemo(
    () => buildLocationTimeline(fixRows, device.status.lastCell?.ts ?? null, undefined, windowDays),
    [fixRows, device, windowDays]
  );

  // Focus is the clicked entry, else the newest one with a position (never the coordinate-less
  // "position unknown" row).
  const autoFocusId = useMemo(() => entries.find((e) => e.lat != null)?.id ?? null, [entries]);
  const focusedId = manualFocusId ?? autoFocusId;
  const focusedEntry = useMemo(() => entries.find((e) => e.id === focusedId) ?? null, [entries, focusedId]);

  // oldest to newest, coordinates only
  const trail = useMemo(
    () =>
      entries
        .filter((e) => e.lat != null && e.lon != null)
        .map((e) => ({ lat: e.lat as number, lon: e.lon as number }))
        .reverse(),
    [entries]
  );

  const backoffLabel = locBackoffLabel(device.status.locBackoffS);

  async function handleLocate() {
    if (!ownerAlias) return;
    setLocateBusy(true);
    try {
      const resp = await api.post<{ requestId: string | null; cached: boolean }>(
        `/conversations/${encodeURIComponent(ownerAlias)}/locate`
      );
      setSnack(
        resp.cached
          ? "Answered from a recent fix -- see the timeline."
          : "Location requested -- watch the timeline for the reply."
      );
    } catch (err) {
      setSnack(
        err instanceof ApiError ? String(err.detail ?? "Location request failed.") : "Location request failed."
      );
    } finally {
      setLocateBusy(false);
    }
  }

  const mapArea =
    entries.length === 0 ? (
      <Alert severity="info">No location fixes in the last 7 days.</Alert>
    ) : focusedEntry && focusedEntry.lat != null && focusedEntry.lon != null ? (
      <LocationMap
        lat={focusedEntry.lat}
        lon={focusedEntry.lon}
        accM={focusedEntry.accM}
        src={focusedEntry.src}
        trail={trail.length > 1 ? trail : undefined}
        height={MAP_HEIGHT_PX}
      />
    ) : (
      <Alert severity="info">
        No map position yet -- the latest report was cell-only with no resolvable position.
      </Alert>
    );

  return (
    <Stack spacing={2} sx={{ flexGrow: 1, minWidth: 0 }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
        {isMobile && (
          <IconButton onClick={onBack} aria-label="back to people list">
            <ArrowBackIcon />
          </IconButton>
        )}
        <Typography variant="h6">
          {device.isOwn ? `You · ${deviceName(device)}` : ownerAlias ? `@${ownerAlias}` : deviceName(device)}
        </Typography>
        <Box sx={{ flexGrow: 1 }} />
        <Stack spacing={0.25} sx={{ alignItems: "flex-end" }}>
          {/* hidden, not disabled, when `canLocate` is false */}
          {device.canLocate && (
            <Button
              size="small"
              variant="outlined"
              startIcon={<LocationOnIcon />}
              disabled={locateBusy || !ownerAlias}
              onClick={() => void handleLocate()}
            >
              Locate now
            </Button>
          )}
          {backoffLabel && (
            <Typography variant="caption" color="text.secondary">
              {backoffLabel}
            </Typography>
          )}
          {device.canLocate && !ownerAlias && (
            <Typography variant="caption" color="text.secondary">
              can&apos;t request yet -- alias unknown
            </Typography>
          )}
        </Stack>
      </Stack>

      <Stack direction={isMobile ? "column" : "row"} spacing={2} sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box
          sx={{
            width: isMobile ? "100%" : 320,
            flexShrink: 0,
            height: isMobile ? MOBILE_TIMELINE_HEIGHT_PX : MAP_HEIGHT_PX,
            // mobile: map first, timeline below
            order: isMobile ? 2 : 0,
          }}
        >
          <LocationTimeline entries={entries} selectedId={focusedId} onSelect={(e) => setManualFocusId(e.id)} />
        </Box>
        <Box sx={{ flexGrow: 1, minWidth: 0, order: 1 }}>{mapArea}</Box>
      </Stack>

      <Snackbar open={snack !== null} autoHideDuration={5000} onClose={() => setSnack(null)} message={snack} />
    </Stack>
  );
}

function LocationInner() {
  const { uidToAlias } = useDirectory();
  const { me } = useAuth();
  // `/locate` is addressed by the owner's alias; for own devices that is `me.alias`.
  const aliasFor = (d: LocatableDevice) => (d.isOwn ? me?.alias : uidToAlias(d.ownerUid));
  const { devices, loaded } = useLocatableDevices();
  // MUI's `md` breakpoint (900 px)
  const isMobile = useMediaQuery("(max-width:899.95px)");

  // deep-link value, read once
  const [initialWho] = useState<string | null>(readWhoParam);

  // `undefined` = no pick yet (use the deep link); `null` = cleared by the back arrow.
  const [manualSelectedId, setManualSelectedId] = useState<string | null | undefined>(undefined);
  const autoSelectedId = useMemo(() => {
    if (!initialWho) return null;
    // best effort: a non-admin's directory may not know the alias yet
    return devices.find((d) => !d.isOwn && uidToAlias(d.ownerUid) === initialWho)?.id ?? null;
  }, [initialWho, devices, uidToAlias]);
  const selectedId = manualSelectedId !== undefined ? manualSelectedId : autoSelectedId;

  const selectedDevice = useMemo(
    () => devices.find((d) => d.id === selectedId) ?? null,
    [devices, selectedId]
  );
  const ownerAlias = selectedDevice ? aliasFor(selectedDevice) : undefined;

  const peopleList = (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 1 }}>
          People you can locate
        </Typography>
        <List dense disablePadding>
          {devices.map((d) => (
            <PersonListItem
              key={d.id}
              device={d}
              alias={aliasFor(d)}
              selected={d.id === selectedId}
              onSelect={() => setManualSelectedId(d.id)}
            />
          ))}
        </List>
      </CardContent>
    </Card>
  );

  return (
    <Stack spacing={2}>
      <Typography variant="h5">Location</Typography>

      {!loaded && <CircularProgress />}

      {loaded && devices.length === 0 && (
        <Alert severity="info">
          You have no device of your own and no one has authorized you to see their location yet. Ask your
          admin to grant it from the allow-list (&quot;Locate&quot; column).
        </Alert>
      )}

      {loaded &&
        devices.length > 0 &&
        (isMobile ? (
          selectedDevice ? (
            <LocationDetail
              key={selectedDevice.id}
              device={selectedDevice}
              ownerAlias={ownerAlias}
              isMobile={isMobile}
              onBack={() => setManualSelectedId(null)}
            />
          ) : (
            peopleList
          )
        ) : (
          <Stack direction="row" spacing={2} sx={{ alignItems: "flex-start" }}>
            <Box sx={{ width: 280, flexShrink: 0 }}>{peopleList}</Box>
            {selectedDevice ? (
              <LocationDetail
                key={selectedDevice.id}
                device={selectedDevice}
                ownerAlias={ownerAlias}
                isMobile={isMobile}
                onBack={() => setManualSelectedId(null)}
              />
            ) : (
              <Alert severity="info" sx={{ flexGrow: 1 }}>
                Select someone on the left to see their location.
              </Alert>
            )}
          </Stack>
        ))}
    </Stack>
  );
}

export default function LocationPage() {
  return (
    <RequireAuth>
      <AppShell>
        <LocationInner />
      </AppShell>
    </RequireAuth>
  );
}
