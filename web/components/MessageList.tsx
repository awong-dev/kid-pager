"use client";

/** Message list pane for the chat thread and monitor routes; scroll state stays with the caller. */

import Link from "next/link";
import type { RefObject, UIEvent } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import LocationOnIcon from "@mui/icons-material/LocationOn";

import DeliveryChips from "@/components/DeliveryChips";
import { formatClock, isLocReqExpired } from "@/lib/time";
import type { MessageDoc } from "@/lib/types";

export interface MessageRow extends MessageDoc {
  id: string;
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
  return <LocRequestLinkRow alias={alias} label={`${mine ? "Location you shared" : "Location received"} -- view`} />;
}

function MessageBubble({
  message,
  mine,
  isGroup,
}: {
  message: MessageRow;
  mine: boolean;
  isGroup: boolean;
}) {
  const atMs = (message.ts ?? 0) * 1000;
  return (
    <Stack sx={{ alignItems: mine ? "flex-end" : "flex-start", my: 0.75 }}>
      {isGroup && message.senderAlias && (
        <Typography variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
          {message.senderAlias}
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
  /** Whether to show the "Load older" button. */
  canLoadOlder: boolean;
  onLoadOlder: () => void;
  listRef: RefObject<HTMLDivElement | null>;
  onScroll: (event: UIEvent<HTMLDivElement>) => void;
  showNewMessagesChip: boolean;
  onJumpToBottom: () => void;
}

export default function MessageList({
  messages,
  meUid,
  isGroup,
  locationAlias,
  canLoadOlder,
  onLoadOlder,
  listRef,
  onScroll,
  showNewMessagesChip,
  onJumpToBottom,
}: MessageListProps) {
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
          if (m.kind === "loc_req") {
            return <LocReqRow key={m.id} message={m} mine={mine} alias={locationAlias} />;
          }
          if (m.kind === "loc") {
            return <LocMessageRow key={m.id} message={m} mine={mine} alias={locationAlias} />;
          }
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
