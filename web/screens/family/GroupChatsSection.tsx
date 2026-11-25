"use client";

/** "Group chats" on `/family/people`: one compact row per member who has a bridge phone, with how
 * many Google Chat / Voice conversations are subscribed, linking to the per-member detail screen
 * `/family/chat?uid=`. One `GET /api/family/bridges` plus one `GET .../members/{uid}/chat` per
 * owner with a paired phone, once per visit (the detail screen is the one that polls). */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Skeleton from "@mui/material/Skeleton";
import Typography from "@mui/material/Typography";

import SectionBlock from "@/components/SectionBlock";
import { ApiError } from "@/lib/api";
import { getChatTab, listBridges } from "@/lib/bridges";
import { useFamily } from "@/lib/family-context";
import { Link } from "@/lib/router";
import { type GroupOwner, groupOwners, subscribedLine } from "./groupChatsModel";

const detailHref = (uid: string): string => `/family/chat?uid=${encodeURIComponent(uid)}`;

export default function GroupChatsSection({ onSettled }: { onSettled?: () => void }) {
  const { familyId } = useFamily();
  const [owners, setOwners] = useState<GroupOwner[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listBridges()
      .then((rows) => {
        if (!live) return;
        const list = groupOwners(rows);
        setOwners(list);
        setError(null);
        onSettled?.();
        for (const o of list.filter((x) => x.paired > 0)) {
          getChatTab(o.uid)
            .then((t) => live && setCounts((c) => ({ ...c, [o.uid]: t.subscribed.length })))
            .catch(() => undefined); // no count for this member; the row still links through
        }
      })
      .catch((err) => {
        if (!live) return;
        setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Could not load bridge phones");
        onSettled?.();
      });
    return () => {
      live = false;
    };
    // `onSettled` is only the page's one-shot signal
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [familyId]);

  const paired = (owners ?? []).filter((o) => o.paired > 0);
  const only = paired.length === 1 ? paired[0]! : null;

  return (
    <SectionBlock
      id="groups"
      title="Group chats"
      description="Google Chat and Voice conversations a member's bridge phone passes on to their pager."
      action={
        paired.length > 0 && (
          <Button variant="outlined" component={Link} href={only ? detailHref(only.uid) : "/family/chat"}>
            Add a group chat
          </Button>
        )
      }
    >
      {error && <Alert severity="error">{error}</Alert>}
      {owners === null && !error && <Skeleton variant="rounded" height={64} />}
      {owners !== null && owners.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          No one has a bridge phone yet. Add one under <Link href="/family/devices">Pagers</Link>.
        </Typography>
      )}
      {owners !== null && owners.length > 0 && (
        <List disablePadding sx={{ border: 1, borderColor: "divider", borderRadius: 1 }}>
          {owners.map((o) => {
            const line = subscribedLine(o, counts[o.uid] ?? null);
            return (
              <ListItem key={o.uid} disablePadding divider>
                <ListItemButton component={Link} href={detailHref(o.uid)}>
                  <ListItemText primary={o.name} secondary={line} />
                </ListItemButton>
              </ListItem>
            );
          })}
        </List>
      )}
    </SectionBlock>
  );
}
