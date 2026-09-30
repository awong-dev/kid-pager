"use client";

/** `/chat/[alias]`: one conversation thread (messages, composer, delivery chips, location link).
 * The alias comes from `usePathname()` because the route is a static shell. */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import LocationOnIcon from "@mui/icons-material/LocationOn";
import SendIcon from "@mui/icons-material/Send";

import AppShell from "@/components/AppShell";
import MessageList, { type MessageRow } from "@/components/MessageList";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";

const PAGE_SIZE_STEP = 50;
// within this distance of the bottom counts as at-bottom
const AT_BOTTOM_THRESHOLD_PX = 80;

// `useLayoutEffect` warns during the static-export prerender.
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

/** First and last `seq`; the scroll effect diffs it to tell append from prepend. */
interface SeqRange {
  first: number;
  last: number;
}

function seqRangeOf(messages: MessageRow[]): SeqRange | null {
  if (messages.length === 0) return null;
  return { first: messages[0]!.seq, last: messages[messages.length - 1]!.seq };
}

/** Collapses a group message's per-recipient copies (shared `groupMsgId`) to the first. */
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

function utf8ByteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

function useAliasFromPath(): string | null {
  const pathname = usePathname();
  return useMemo(() => {
    if (!pathname) return null;
    const parts = pathname.split("/").filter(Boolean);
    if (parts.length < 2 || parts[0] !== "chat") return null;
    return decodeURIComponent(parts[1]!);
  }, [pathname]);
}

function ThreadInner({ alias }: { alias: string }) {
  const { familyId } = useFamily();
  const router = useRouter();
  const peerUid = aliasToUid(alias);
  // DM and group aliases are disjoint server-side: at most one of `peerUid`/`group` is set.
  const group = groupByAlias(alias);

  const [pageSize, setPageSize] = useState(PAGE_SIZE_STEP);

  const [composer, setComposer] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);
  const [snack, setSnack] = useState<string | null>(null);
  const [showNewMessagesChip, setShowNewMessagesChip] = useState(false);

  const markedReadRef = useRef<Set<string>>(new Set());

  const listRef = useRef<HTMLDivElement | null>(null);
  // a ref: updated on every scroll event without needing a re-render
  const atBottomRef = useRef(true);
  const prevSeqRef = useRef<SeqRange | null>(null);
  // scrollHeight just before "Load older" grows the list, for the scroll-position correction
  const prevScrollHeightRef = useRef<number | null>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");

  const convKey = group ? group.convKey : me && peerUid ? [me.uid, peerUid].sort().join("_") : null;

  // `pageSize` grows on "load older" and the listener re-subscribes with the bigger limit.

  // Peer's pager device, for the status line and "View location" link. An admin queries any device;
  // a member must filter on their own uid in `locatableBy`. Both need `familyId` for the rules.

  const canViewLocation = Boolean(me && device?.locatableBy.includes(me.uid));

  // Marks unread webapp deliveries read, only while at the bottom and the tab is visible.
  // The visibility listener calls it through `markReadRef` to avoid a stale closure.
  function markRead() {
    if (!me) return;
    if (!atBottomRef.current || document.visibilityState !== "visible") return;
    for (const msg of messages) {
      if (msg.recipientUid !== me.uid) continue;
      if (markedReadRef.current.has(msg.id)) continue;
      const webapp = Object.values(msg.deliveries).find((d) => d.kind === "webapp");
      if (!webapp || webapp.state === "read") continue;
      markedReadRef.current.add(msg.id);
      void api.post(`/conversations/${encodeURIComponent(alias)}/messages/${msg.id}/read`).catch(() => {
        markedReadRef.current.delete(msg.id);
      });
    }
  }
  const markReadRef = useRef(markRead);
  useEffect(() => {
    markReadRef.current = markRead;
  });

  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState === "visible") markReadRef.current();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  // Every "reached the bottom" transition goes through here, so mark-read is rechecked.
  function scrollToBottom(behavior: ScrollBehavior) {
    const el = listRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    atBottomRef.current = true;
    setShowNewMessagesChip(false);
    markRead();
  }
  // via a ref so the layout effect does not re-run on every render
  const scrollToBottomRef = useRef(scrollToBottom);
  useEffect(() => {
    scrollToBottomRef.current = scrollToBottom;
  });

  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    const el = event.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD_PX;
    atBottomRef.current = atBottom;
    if (atBottom) {
      setShowNewMessagesChip(false);
      markRead();
    }
  }

  // Auto-scroll before paint so the position never flashes wrong.
  useIsomorphicLayoutEffect(() => {
    const el = listRef.current;
    const prev = prevSeqRef.current;
    const next = seqRangeOf(messages);

    if (el && next) {
      if (!prev) {
        // first snapshot: jump to the bottom
        scrollToBottomRef.current("auto");
      } else if (next.last > prev.last) {
        // append: follow if at the bottom or own message, else offer the chip
        const lastMessage = messages[messages.length - 1]!;
        if (atBottomRef.current || lastMessage.senderUid === me?.uid) {
          scrollToBottomRef.current(reducedMotion ? "auto" : "smooth");
        } else {
          setShowNewMessagesChip(true);
        }
      } else if (next.first < prev.first) {
        // prepend: keep the viewer's place; the anchor is cleared only here because the
        // re-subscribed listener can fire twice (cache, then server) for the same query
        const prevScrollHeight = prevScrollHeightRef.current;
        if (prevScrollHeight !== null) {
          el.scrollTop += el.scrollHeight - prevScrollHeight;
          prevScrollHeightRef.current = null;
        }
      }
    }

    prevSeqRef.current = next;
  }, [messages, me, reducedMotion]);

  const bodyBytes = utf8ByteLength(composer);
  const bodyValid =
    composer.trim().length > 0 &&
    bodyBytes <= BODY_MAX_UTF8_BYTES;

  async function handleSend() {
    if (!bodyValid || sending) return;
    setSending(true);
    setSendError(null);
    try {
      const resp = await api.post<{ id: string }>(
        `/conversations/${encodeURIComponent(alias)}/messages`,
        { body: composer }
      );
      setComposer("");
      // learn the peer uid from the sent message; not for a group (copies name arbitrary members)
      if (!peerUid && !group) {
        }
      }
    } catch (err) {
      // a policy-blocked send is 403 `{reason, message}`; `ApiError` keeps `message`
      setSendError(err instanceof ApiError ? err.message : "Failed to send");
    } finally {
      setSending(false);
    }
  }

  // `DELETE /api/conversations/{alias}/members/me`
  async function handleLeave() {
    if (!group || leaving) return;
    if (!window.confirm(`Leave "${group.name}"? You'll keep read access to messages you already received.`)) {
      return;
    }
    setLeaving(true);
    try {
      await api.del(`/conversations/${encodeURIComponent(alias)}/members/me`);
      router.push("/chat");
    } catch (err) {
      setSnack(err instanceof ApiError ? String(err.detail ?? "Failed to leave group.") : "Failed to leave group.");
    } finally {
      setLeaving(false);
    }
  }

  return (
        {group && (
          <Typography variant="caption" color="text.secondary">
            @{alias}
          </Typography>
        )}
        {device && (
          <Typography variant="caption" color="text.secondary">
            pager: {device.status.state ?? "unknown"}
          </Typography>
        )}
        <Box sx={{ flexGrow: 1 }} />
          <Button size="small" color="inherit" disabled={leaving} onClick={() => void handleLeave()}>
            Leave
          </Button>
        )}
        {canViewLocation && (
          <Button
            size="small"
            variant="outlined"
            startIcon={<LocationOnIcon />}
            component={Link}
            href={`/location?who=${encodeURIComponent(alias)}`}
          >
            View location
          </Button>
        )}
      </Stack>

      {!peerUid && !group && <Alert severity="info">No conversation with @{alias} yet</Alert>}

      <MessageList
        messages={messages}
        meUid={me?.uid ?? null}
        isGroup={Boolean(group)}
        locationAlias={alias}
        canLoadOlder={Boolean(convKey) && messages.length >= pageSize}
        onLoadOlder={() => {
          // record the pre-prepend scrollHeight for the layout effect
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

      <Stack direction="row" spacing={1} sx={{ alignItems: "flex-end" }}>
        <TextField
          fullWidth
          multiline
          maxRows={4}
          placeholder="Message"
          value={composer}
          onChange={(e) => setComposer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void handleSend();
            }
          }}
        />
        <Button
          variant="contained"
          endIcon={sending ? <CircularProgress size={16} color="inherit" /> : <SendIcon />}
          disabled={!bodyValid || sending}
          onClick={() => void handleSend()}
        >
          Send
        </Button>
      </Stack>
      {sendError && <Alert severity="error">{sendError}</Alert>}

      <Snackbar
        open={snack !== null}
        autoHideDuration={5000}
        onClose={() => setSnack(null)}
        message={snack}
      />
    </Stack>
  );
}

export default function ThreadPageClient() {
  const alias = useAliasFromPath();
  return (
    <RequireAuth>
      <AppShell>
        {/* key remounts ThreadInner so local state resets per alias */}
        {alias ? <ThreadInner key={alias} alias={alias} /> : <CircularProgress />}
      </AppShell>
    </RequireAuth>
  );
}
