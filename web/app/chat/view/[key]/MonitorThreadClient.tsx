"use client";

/** `/chat/view/[key]`: a family admin's read-only view of a conversation they are not a party to.
 * Names come from `conversations/{key}.participants`; `byUid()` finds the party in their family. */

import { usePathname } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import VisibilityIcon from "@mui/icons-material/Visibility";

import AppShell from "@/components/AppShell";
import MessageList, { type MessageRow } from "@/components/MessageList";
import RequireAuth from "@/components/RequireAuth";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import type { ConversationDoc, MessageDoc } from "@/lib/types";

const PAGE_SIZE_STEP = 50;
// within this distance of the bottom counts as at-bottom
const AT_BOTTOM_THRESHOLD_PX = 80;

// `useLayoutEffect` warns during the static-export prerender.
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

/** First and last `seq` of the message array. */
interface SeqRange {
  first: number;
  last: number;
}

function seqRangeOf(messages: MessageRow[]): SeqRange | null {
  if (messages.length === 0) return null;
  return { first: messages[0]!.seq, last: messages[messages.length - 1]!.seq };
}

// Collapses a group message's per-recipient copies (shared `groupMsgId`) to the first.
function dedupeByGroupMsgId(rows: MessageRow[]): MessageRow[] {
  const seen = new Set<string>();
  const result: MessageRow[] = [];
  for (const row of rows) {
    const key = row.groupMsgId ?? row.id;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(row);
  }
  return result;
}

function useKeyFromPath(): string | null {
  const pathname = usePathname();
  return useMemo(() => {
    if (!pathname) return null;
    const parts = pathname.split("/").filter(Boolean);
    if (parts.length < 3 || parts[0] !== "chat" || parts[1] !== "view") return null;
    return decodeURIComponent(parts[2]!);
  }, [pathname]);
}

function MonitorThreadInner({ convKey }: { convKey: string }) {
  const { byUid } = useDirectory();
  const { familyId } = useFamily();

  const [pageSize, setPageSize] = useState(PAGE_SIZE_STEP);
  const [showNewMessagesChip, setShowNewMessagesChip] = useState(false);

  const listRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  const prevSeqRef = useRef<SeqRange | null>(null);
  const prevScrollHeightRef = useRef<number | null>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");



  function scrollToBottom(behavior: ScrollBehavior) {
    const el = listRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    atBottomRef.current = true;
    setShowNewMessagesChip(false);
  }
  const scrollToBottomRef = useRef(scrollToBottom);
  useEffect(() => {
    scrollToBottomRef.current = scrollToBottom;
  });

  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    const el = event.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD_PX;
    atBottomRef.current = atBottom;
    if (atBottom) setShowNewMessagesChip(false);
  }

  useIsomorphicLayoutEffect(() => {
    const el = listRef.current;
    const prev = prevSeqRef.current;
    const next = seqRangeOf(messages);

    if (el && next) {
      if (!prev) {
        scrollToBottomRef.current("auto");
      } else if (next.last > prev.last) {
        if (atBottomRef.current) {
          scrollToBottomRef.current(reducedMotion ? "auto" : "smooth");
        } else {
          setShowNewMessagesChip(true);
        }
      } else if (next.first < prev.first) {
        const prevScrollHeight = prevScrollHeightRef.current;
        if (prevScrollHeight !== null) {
          el.scrollTop += el.scrollHeight - prevScrollHeight;
          prevScrollHeightRef.current = null;
        }
      }
    }

    prevSeqRef.current = next;
  }, [messages, reducedMotion]);

  const isGroup = conversation?.kind === "group";
  const participants = conversation?.participants ?? {};
  const uids = conversation?.uids ?? [];
  // "kid" is the party in my own family; falls back to the first uid.
  const kidUid = uids.find((u) => byUid(u)?.familyId === familyId) ?? uids[0] ?? null;
  const peerUid = uids.find((u) => u !== kidUid) ?? null;
  function label(uid: string | null): string {
    if (!uid) return "someone";
  }
  const banner = isGroup
    ? `You're viewing the "${conversation?.name ?? conversation?.alias ?? convKey}" group as a family admin.`

  return (
      <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
        <VisibilityIcon color="action" fontSize="small" />
        <Typography variant="subtitle2" color="text.secondary">
          {banner}
        </Typography>
        {!conversation && <CircularProgress size={16} />}
      </Stack>

      <MessageList
        messages={messages}
        meUid={kidUid}
        isGroup={isGroup}
        canLoadOlder={messages.length >= pageSize}
        onLoadOlder={() => {
          if (listRef.current) {
            prevScrollHeightRef.current = listRef.current.scrollHeight;
          }
          setPageSize((n) => n + PAGE_SIZE_STEP);
        }}
        listRef={listRef}
        onScroll={handleScroll}
        showNewMessagesChip={showNewMessagesChip}
        onJumpToBottom={() => scrollToBottom(reducedMotion ? "auto" : "smooth")}
      />
    </Stack>
  );
}

export default function MonitorThreadClient() {
  const convKey = useKeyFromPath();
  return (
    <RequireAuth requireRole="admin">
      <AppShell>
        {/* key remounts per conversation */}
        {convKey ? <MonitorThreadInner key={convKey} convKey={convKey} /> : <CircularProgress />}
      </AppShell>
    </RequireAuth>
  );
}
