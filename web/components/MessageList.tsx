"use client";

/** Message list pane for the chat thread and monitor routes; scroll state stays with the caller. */

import type { RefObject, UIEvent } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import LocationOnIcon from "@mui/icons-material/LocationOn";

import { Link } from "@/lib/router";
import DeliveryChips from "@/components/DeliveryChips";
import { useAuth } from "@/lib/auth-context";
import SmsLogBubble from "@/components/SmsLogBubble";
import type { SmsLogEntry } from "@/lib/smsContacts";
import { formatClock, isLocReqExpired } from "@/lib/time";
import type { MessageDoc } from "@/lib/types";

export interface MessageRow extends MessageDoc {
  id: string;
}

/** Side is decided by origin: a device and its owner's browser share one `senderUid`, so
 * `originBackendKind === "pager"` is never "mine". */
function isFromPager(m: MessageDoc): boolean {
  return m.originBackendKind === "pager";
}

// `loc_req` and `kind='loc'` messages render as one small link to `/location`; the read-only
// monitor thread omits `alias` and gets plain text.
function LocRequestLinkRow({ alias, label }: { alias?: string; label: string }) {
  const content = (
    <Typography variant="caption" color="text.secondary">
      <LocationOnIcon fontSize="inherit" sx={{ verticalAlign: "middle" }} /> {label}
    </Typography>
  );
  const boxSx = {
    px: 1.5,
    py: 0.5,
    border: 1,
    borderColor: "divider",
    borderRadius: 2,
  } as const;
  return (
    <Stack direction="row" sx={{ justifyContent: "center", my: 1 }}>
      {alias ? (
        <Box
          component={Link}
          href={`/location?who=${encodeURIComponent(alias)}`}
          sx={{ ...boxSx, textDecoration: "none", color: "inherit" }}
        >
          {content}
        </Box>
      ) : (
        <Box sx={boxSx}>{content}</Box>
      )}
    </Stack>
  );
}

function LocReqRow({ message, mine, alias }: { message: MessageRow; mine: boolean; alias?: string }) {
  const delivery = Object.values(message.deliveries)[0];
  const expired = isLocReqExpired(message);
  const state =
    expired && delivery?.state !== "fulfilled" ? "expired" : (delivery?.state ?? "queued");
  return (
    <LocRequestLinkRow
      alias={alias}
      label={`${mine ? "You requested a location" : "Location requested"} -- ${state} -- view`}
    />
  );
}

function LocMessageRow({ message, mine, alias }: { message: MessageRow; mine: boolean; alias?: string }) {
  if (message.locExpired || !message.loc) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ my: 0.75, textAlign: mine ? "right" : "left" }}>
        location (expired)
      </Typography>
    );
  }
  return <LocRequestLinkRow alias={alias} label={`${mine ? "Location you shared" : "Location received"} -- view`} />;
}

function MessageBubble({
  message,
  mine,
  isGroup,
  meUid,
  hookNames,
}: {
  message: MessageRow;
  mine: boolean;
  isGroup: boolean;
  meUid: string | null;
  hookNames?: Record<string, string>;
}) {
  const atMs = (message.ts ?? 0) * 1000;
  const { isFamilyAdmin } = useAuth();
  // Hook labels/reason: family admins only.
  const hookNotes = isFamilyAdmin ? Object.entries(message.hooks ?? {}) : [];
  return (
    <Stack sx={{ alignItems: mine ? "flex-end" : "flex-start", my: 0.75 }}>
      {isGroup && message.senderAlias && (
        <Typography variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
          {message.senderAlias}
          {isFromPager(message) ? " (pager)" : ""}
        </Typography>
      )}
      {!isGroup && isFromPager(message) && message.senderUid === meUid && (
        <Typography variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
          your pager
        </Typography>
      )}
      <Box
        sx={{
          px: 1.5,
          py: 1,
          maxWidth: "80%",
          borderRadius: 2,
          bgcolor: mine ? "primary.main" : "grey.100",
          color: mine ? "primary.contrastText" : "text.primary",
        }}
      >
        <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {message.body}
        </Typography>
        <Typography variant="caption" sx={{ opacity: 0.7, display: "block", mt: 0.25 }}>
          {formatClock(atMs)}
        </Typography>
      </Box>
      {hookNotes.map(([hookId, h]) => {
        const labels = h.labels ?? [];
        if (labels.length === 0 && !h.reason && (h.action === "allow" || !h.action)) return null;
        return (
          <Stack key={hookId} direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center", mt: 0.25 }}>
            {hookNames?.[hookId] && (
              <Typography variant="caption" color="text.secondary">
                {hookNames[hookId]}
              </Typography>
            )}
            {h.action && h.action !== "allow" && <Chip size="small" color="warning" variant="outlined" label={h.action} />}
            {labels.map((l) => (
              <Chip key={l} size="small" variant="outlined" label={l} />
            ))}
            {h.reason && (
              <Typography variant="caption" color="text.secondary">
                {h.reason}
              </Typography>
            )}
          </Stack>
        );
      })}
      {message.senderUid === meUid && <DeliveryChips message={message} />}
    </Stack>
  );
}

export interface MessageListProps {
  messages: MessageRow[];
  /** Uid whose messages render right-aligned ("mine"). */
  meUid: string | null;
  isGroup: boolean;
  /** Alias for the loc rows' `/location?who=` link; omit for plain text. */
  locationAlias?: string;
  /** Hook id -> name (the family's hooks list), for the hook notes. */
  hookNames?: Record<string, string>;
  /** Whether to show the "Load older" button. */
  canLoadOlder: boolean;
  onLoadOlder: () => void;
  listRef: RefObject<HTMLDivElement | null>;
  onScroll: (event: UIEvent<HTMLDivElement>) => void;
  showNewMessagesChip: boolean;
  onJumpToBottom: () => void;
  /** Pager-modem SMS audit rows to interleave by time (SMS-contact threads). */
  modemRows?: SmsLogEntry[];
}

export default function MessageList({
  messages,
  meUid,
  isGroup,
  locationAlias,
  hookNames,
  canLoadOlder,
  onLoadOlder,
  listRef,
  onScroll,
  showNewMessagesChip,
  onJumpToBottom,
  modemRows,
}: MessageListProps) {
  const items = [
    ...messages.map((m) => ({ ts: m.ts ?? 0, m, e: null as SmsLogEntry | null })),
    ...(modemRows ?? []).map((e) => ({ ts: e.smsTs || e.ts, m: null as MessageRow | null, e })),
  ].sort((a, b) => a.ts - b.ts);
  return (
    // `position: relative` is on this wrapper, not the scrolling Box, so the chip does not scroll
    // with the content; `minHeight: 0` lets the inner `overflowY: auto` box do the scrolling.
    <Box sx={{ position: "relative", flexGrow: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Box ref={listRef} onScroll={onScroll} sx={{ flexGrow: 1, overflowY: "auto", px: 1 }}>
        {canLoadOlder && (
          <Stack direction="row" sx={{ justifyContent: "center", mb: 1 }}>
            <Button size="small" onClick={onLoadOlder}>
              Load older
            </Button>
          </Stack>
        )}
        {items.map(({ m, e }) => {
          if (!m) return <SmsLogBubble key={`sms-${e!.id}`} entry={e!} />;
          const mine = m.senderUid === meUid && !isFromPager(m);
          if (m.kind === "loc_req") {
            return <LocReqRow key={m.id} message={m} mine={mine} alias={locationAlias} />;
          }
          if (m.kind === "loc") {
            return <LocMessageRow key={m.id} message={m} mine={mine} alias={locationAlias} />;
          }
          return <MessageBubble key={m.id} message={m} mine={mine} isGroup={isGroup} meUid={meUid} hookNames={hookNames} />;
        })}
      </Box>
      {showNewMessagesChip && (
        <Chip
          label="New messages ↓"
          color="primary"
          onClick={onJumpToBottom}
          sx={{
            position: "absolute",
            bottom: 8,
            left: "50%",
            transform: "translateX(-50%)",
            cursor: "pointer",
            boxShadow: 2,
          }}
        />
      )}
    </Box>
  );
}
