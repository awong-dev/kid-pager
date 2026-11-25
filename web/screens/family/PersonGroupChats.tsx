"use client";

/** The "Group chats" section of one person's permissions: how many Google Chat / Voice conversations
 * their bridge phone passes on (`GET /api/family/members/{uid}/chat`, once) and a link to the detail
 * screen `/family/chat?uid=`, where they are subscribed and ignored. */

import { useEffect, useState } from "react";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import SectionBlock from "@/components/SectionBlock";
import { getChatTab } from "@/lib/bridges";
import { Link } from "@/lib/router";

export default function PersonGroupChats({ uid, name }: { uid: string; name: string }) {
  // `null` = still loading; `false` = no count (no paired bridge phone, or the call failed)
  const [count, setCount] = useState<number | null | false>(null);
  useEffect(() => {
    let live = true;
    getChatTab(uid)
      .then((t) => live && setCount(t.subscribed.length))
      .catch(() => live && setCount(false));
    return () => {
      live = false;
    };
  }, [uid]);

  const line =
    count === null
      ? "Checking..."
      : count === false
        ? `${name} has no bridge phone paired yet, so there are no group chats.`
        : count === 0
          ? `No group chats subscribed for ${name} yet.`
          : `${count} group ${count === 1 ? "chat" : "chats"} subscribed for ${name}.`;

  return (
    <SectionBlock
      id="perm-groups"
      title="Group chats"
      description="Google Chat and Voice conversations their bridge phone passes on to their pager."
    >
      <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
        <Typography variant="body2">{line}</Typography>
        <Button variant="outlined" component={Link} href={`/family/chat?uid=${encodeURIComponent(uid)}`}>
          Group chats...
        </Button>
      </Stack>
    </SectionBlock>
  );
}
