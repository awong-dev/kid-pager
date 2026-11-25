"use client";

/** `/`: the launcher. A card per place the viewer can go (data: `./cards.ts`), each with one live
 * status line. Reads only what the viewer's role may read (`relay/firestore.rules`): family-wide
 * queries (members, alerts) are admin-only; a plain member's pager status comes from the devices
 * they own. */

import { type ReactNode, useEffect, useMemo, useState } from "react";
import AdminPanelSettingsOutlinedIcon from "@mui/icons-material/AdminPanelSettingsOutlined";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import Chip from "@mui/material/Chip";
import ForumOutlinedIcon from "@mui/icons-material/ForumOutlined";
import GroupsOutlinedIcon from "@mui/icons-material/GroupsOutlined";
import NotificationsActiveOutlinedIcon from "@mui/icons-material/NotificationsActiveOutlined";
import NotificationsNoneOutlinedIcon from "@mui/icons-material/NotificationsNoneOutlined";
import SettingsOutlinedIcon from "@mui/icons-material/SettingsOutlined";
import SettingsRemoteOutlinedIcon from "@mui/icons-material/SettingsRemoteOutlined";
import VerifiedUserOutlinedIcon from "@mui/icons-material/VerifiedUserOutlined";
import Skeleton from "@mui/material/Skeleton";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";

import { useOpenAlerts, useShellDevices } from "@/components/shell/ShellData";
import { useAuth } from "@/lib/auth-context";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { familyMembersQuery, myConversationsQuery } from "@/lib/live/queries";
import { currentPushPath } from "@/lib/notifications";
import { Link } from "@/lib/router";
import { formatRelativeAge, tsToMillis } from "@/lib/time";
import type { ConversationDoc, UserDoc } from "@/lib/types";

import {
  CARDS,
  type CardId,
  type HomeCard,
  type IconKey,
  type Status,
  cardVisible,
  myPagerStatus,
  onlineLine,
  pagersStatus,
  plural,
  unreadStatus,
} from "./cards";

const ICONS: Record<IconKey, ReactNode> = {
  forum: <ForumOutlinedIcon fontSize="medium" />,
  alerts: <NotificationsActiveOutlinedIcon fontSize="medium" />,
  groups: <GroupsOutlinedIcon fontSize="medium" />,
  verified: <VerifiedUserOutlinedIcon fontSize="medium" />,
  remote: <SettingsRemoteOutlinedIcon fontSize="medium" />,
  settings: <SettingsOutlinedIcon fontSize="medium" />,
  admin: <AdminPanelSettingsOutlinedIcon fontSize="medium" />,
  notifications: <NotificationsNoneOutlinedIcon fontSize="medium" />,
};

const LOADING: Status = { kind: "loading" };
const NONE: Status = { kind: "none" };

/** Is this browser registered for push (`lib/notifications.ts` `currentPushPath`)? */
function usePushRegistered(enabled: boolean): "loading" | "failed" | "on" | "off" {
  const [registered, setRegistered] = useState<"loading" | "failed" | "on" | "off">("loading");
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    currentPushPath()
      .then(({ subscribed }) => {
        if (!cancelled) setRegistered(subscribed ? "on" : "off");
      })
      .catch(() => {
        if (!cancelled) setRegistered("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return registered;
}

/** Every card's status, keyed by card id. */
function useStatuses(): Partial<Record<CardId, Status>> {
  const { me, isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  const alerts = useOpenAlerts();
  const shellDevices = useShellDevices();
  const convs = useLive<ConversationDoc>(me ? myConversationsQuery(me.uid) : null);
  const members = useLive<UserDoc>(isFamilyAdmin && familyId ? familyMembersQuery(familyId) : null);
  const push = usePushRegistered(!isFamilyAdmin);
  const uid = me?.uid ?? "";

  return useMemo(() => {
    const out: Partial<Record<CardId, Status>> = {};
    const devices = shellDevices.devices;
    const devicesStatus = (f: () => Status): Status =>
      shellDevices.failed ? NONE : shellDevices.loaded ? f() : LOADING;

    // Messages: unread across my conversations, and how many pagers are online.
    const unread = convs.docs.reduce((n, d) => n + (d.data.unread?.[uid] ?? 0), 0);
    const online = familyId && shellDevices.loaded ? onlineLine(devices) : null;
    out.messages = convs.error
      ? NONE
      : convs.loading
        ? LOADING
        : { kind: "text", text: unreadStatus(unread), second: online ?? undefined };

    if (isFamilyAdmin) {
      out.alerts = alerts.failed
        ? NONE
        : alerts.loading
          ? LOADING
          : alerts.count > 0
            ? { kind: "alerts", count: alerts.count }
            : { kind: "text", text: "Nothing waiting" };
      out.people = members.error
        ? NONE
        : members.loading
          ? LOADING
          : { kind: "text", text: plural(members.docs.length, "family member") };
      out.pagers = devicesStatus(() => ({ kind: "text", text: pagersStatus(devices) }));
    }
    if (!isFamilyAdmin) {
      const own = devices.filter((d) => d.isOwn);
      out.myPager = devicesStatus(() => ({
        kind: "text",
        text: myPagerStatus(own, (d) => tsToMillis(d.status.updatedAt), formatRelativeAge),
      }));
      out.notifications =
        push === "loading" ? LOADING : push === "failed" ? NONE : { kind: "text", text: push === "on" ? "On" : "Off" };
    }
    return out;
  }, [shellDevices, convs.docs, convs.error, convs.loading, uid, familyId, isFamilyAdmin, alerts, members.docs.length, members.error, members.loading, push]);
}

function StatusView({ status, align }: { status: Status; align: "start" | "end" }) {
  if (status.kind === "none") return null;
  if (status.kind === "loading") return <Skeleton variant="text" width={72} />;
  if (status.kind === "alerts") {
    return <Chip size="small" color="error" label={`${status.count} waiting`} />;
  }
  return (
    <Box sx={{ textAlign: align }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {status.text}
      </Typography>
      {status.second && (
        <Typography variant="caption" color="text.secondary" component="div">
          {status.second}
        </Typography>
      )}
    </Box>
  );
}

function HomeCardView({ card, status }: { card: HomeCard; status: Status }) {
  const featured = card.featured === true;
  const urgent = status.kind === "alerts";
  return (
    <Card
      variant="outlined"
      sx={{
        gridColumn: featured ? "1 / -1" : undefined,
        overflow: "visible", // so the focus outline (offset outside the card) is not clipped
        borderColor: urgent ? "error.main" : undefined,
        bgcolor: featured ? (t) => alpha(t.palette.primary.main, 0.06) : undefined,
        "&:hover": { borderColor: "primary.main", bgcolor: "action.hover" },
      }}
    >
      <CardActionArea
        component={Link}
        href={card.href}
        sx={{
          display: "grid",
          alignItems: "center",
          borderRadius: "inherit",
          columnGap: 2,
          rowGap: { sm: 1.5 },
          p: { xs: 2, sm: 2.5 },
          minHeight: { xs: 72, sm: 132 },
          // phone: a row (icon | text | status); sm and up: icon + status on top, text below
          gridTemplateColumns: "40px minmax(0, 1fr) auto",
          gridTemplateAreas: {
            xs: '"icon text status"',
            sm: '"icon . status" "text text text"',
          },
          alignContent: { sm: "start" },
          "&.Mui-focusVisible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: "2px" },
          // the card carries hover; drop the action area's own overlays
          "& .MuiCardActionArea-focusHighlight": { display: "none" },
        }}
      >
        <Box
          aria-hidden
          sx={{
            gridArea: "icon",
            width: 40,
            height: 40,
            borderRadius: "50%",
            display: "grid",
            placeItems: "center",
            color: "primary.main",
            bgcolor: (t) => alpha(t.palette.primary.main, 0.08),
          }}
        >
          {ICONS[card.icon]}
        </Box>
        <Box sx={{ gridArea: "text", minWidth: 0 }}>
          <Typography variant={featured ? "h6" : "subtitle1"} component="div" sx={{ fontWeight: 600 }}>
            {card.title}
          </Typography>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{
              display: { xs: "-webkit-box", sm: "block" },
              WebkitBoxOrient: "vertical",
              WebkitLineClamp: { xs: 2, sm: "unset" },
              overflow: "hidden",
            }}
          >
            {card.description}
          </Typography>
        </Box>
        <Box sx={{ gridArea: "status", maxWidth: { xs: 124, sm: 200 }, justifySelf: "end", alignSelf: { sm: "start" } }}>
          <StatusView status={status} align="end" />
        </Box>
      </CardActionArea>
    </Card>
  );
}

export default function Home() {
  const { me, isFamilyAdmin, isSuper } = useAuth();
  const { familyId } = useFamily();
  const statuses = useStatuses();
  const cards = CARDS.filter((c) => cardVisible(c, { isFamilyAdmin, isSuper }));

  return (
    <>
      <Typography variant="h5" component="h1" sx={{ mb: 2 }}>
        Hi, {me?.displayName ?? ""}
      </Typography>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", md: "repeat(3, 1fr)" },
          gap: { xs: 1.5, sm: 2 },
        }}
      >
        {cards.map((c) => {
          const noFamily = c.needsFamily && !familyId;
          const status: Status = noFamily
            ? { kind: "text", text: "Choose a family" }
            : (statuses[c.id] ?? NONE);
          return <HomeCardView key={c.id} card={c} status={status} />;
        })}
      </Box>
    </>
  );
}
