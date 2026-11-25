"use client";

/** Messages dashboard, "Where they are": one compact card per pager the viewer can see (the devices
 * `useLocatableDevices` finds, shared through the shell). Each shows the person, whether the pager
 * is online, the newest fix (one small `limit(1)` read per card) and, when allowed, "Locate now";
 * the card opens `/location?who=<alias>` for the map and timeline. */

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CircleIcon from "@mui/icons-material/Circle";

import LocateNowButton from "@/components/LocateNowButton";
import { useShellDevices } from "@/components/shell/ShellData";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { deviceName } from "@/lib/devices";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { deviceLocationsQuery } from "@/lib/live/queries";
import type { LocatableDevice } from "@/lib/locatableDevices";
import { Link } from "@/lib/router";
import { formatRelativeAge } from "@/lib/time";
import type { LocationFixDoc } from "@/lib/types";

import { fixLine, pagerLine, personTitle, sortCards } from "./messagesModel";

/** Whether the dashboard has a "Where they are" section: always for an admin, else only when the
 * viewer has a pager of their own or a grant (or while that is still loading). */
export function useShowWhere(): boolean {
  const { isFamilyAdmin } = useAuth();
  const { devices, loaded, failed } = useShellDevices();
  return isFamilyAdmin || !loaded || failed || devices.some((d) => !d.revokedAt);
}

function LatestFix({ deviceId }: { deviceId: string }) {
  const live = useLive<LocationFixDoc>(deviceLocationsQuery(deviceId, 1));
  if (live.error) return <Typography variant="caption" color="text.secondary">Position unavailable</Typography>;
  if (live.loading) return <Skeleton variant="text" width={160} />;
  const fix = live.docs[0]?.data;
  if (!fix) return <Typography variant="caption" color="text.secondary">No position yet</Typography>;
  const { text, stale } = fixLine(fix, formatRelativeAge);
  return (
    <Typography variant="caption" color={stale ? "warning.main" : "text.secondary"}>
      {text}
    </Typography>
  );
}

interface CardModel {
  device: LocatableDevice;
  title: string;
  isOwn: boolean;
  /** Owner's alias; `/locate` and the deep link are addressed by it. */
  alias: string | undefined;
}

function PersonCard({ m }: { m: CardModel }) {
  const { device, alias } = m;
  const line = pagerLine(device.status, formatRelativeAge);
  return (
    <Card variant="outlined" sx={{ "&:hover": { borderColor: "primary.main" } }}>
      <CardActionArea
        component={Link}
        href={alias ? `/location?who=${encodeURIComponent(alias)}` : "/location"}
        sx={{ p: 1.5, "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: "2px" } }}
      >
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }} noWrap>
            {m.title}
          </Typography>
          <Box sx={{ flexGrow: 1 }} />
          <CircleIcon
            aria-hidden
            sx={{ fontSize: 10 }}
            color={line.online ? "success" : "disabled"}
          />
          <Typography variant="body2">{line.text}</Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary" component="div" noWrap>
          {deviceName(device)}
        </Typography>
        <LatestFix deviceId={device.id} />
      </CardActionArea>
      {device.canLocate && alias && (
        <Box sx={{ px: 1.5, pb: 1.5 }}>
          <LocateNowButton alias={alias} />
        </Box>
      )}
    </Card>
  );
}

export default function WhereSection() {
  const { me, isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  const { uidToAlias, byUid } = useDirectory();
  const { devices, loaded, failed } = useShellDevices();

  const cards = sortCards(
    devices
      .filter((d) => !d.revokedAt)
      .map((d): CardModel => {
        const alias = d.isOwn ? me?.alias : uidToAlias(d.ownerUid);
        const title = personTitle({
          isOwn: d.isOwn,
          displayName: byUid(d.ownerUid)?.displayName,
          alias,
          pagerName: deviceName(d),
        });
        return { device: d, title, isOwn: d.isOwn, alias };
      })
  );

  let body;
  if (failed && cards.length === 0) {
    body = <Alert severity="error">Couldn&apos;t load the pagers. Try reloading the page.</Alert>;
  } else if (!familyId) {
    body = (
      <Typography variant="body2" color="text.secondary">
        Choose a family to see where its pagers are.
      </Typography>
    );
  } else if (!loaded) {
    body = <Skeleton variant="rounded" height={120} />;
  } else if (cards.length === 0) {
    body = (
      <Typography variant="body2" color="text.secondary">
        {isFamilyAdmin ? "No pagers yet. Add one under Family, Pagers." : "Nobody to show yet."}
      </Typography>
    );
  } else {
    body = (
      <Stack spacing={1.5}>
        {cards.map((m) => (
          <PersonCard key={m.device.id} m={m} />
        ))}
      </Stack>
    );
  }

  return (
    <Box component="section" aria-labelledby="messages-where" sx={{ minWidth: 0 }}>
      <Typography variant="h6" component="h2" id="messages-where" sx={{ mb: 1 }}>
        Where they are
      </Typography>
      {body}
    </Box>
  );
}
