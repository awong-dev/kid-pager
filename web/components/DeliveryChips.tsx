import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";

import { fedDeliveryLabel } from "@/lib/federation";
import { formatClock, isLocReqExpired } from "@/lib/time";
import type { DeliveryDoc, DeliveryState, MessageDoc } from "@/lib/types";

const STATE_COLOR: Record<DeliveryState, "default" | "info" | "success" | "warning" | "error"> = {
  queued: "default",
  sent: "info",
  shown: "info",
  read: "success",
  fulfilled: "success",
  failed: "error",
  expired: "warning",
};

function effectiveState(msg: MessageDoc, delivery: DeliveryDoc): DeliveryState {
  if (isLocReqExpired(msg) && (delivery.state === "queued" || delivery.state === "sent")) {
    return "expired";
  }
  return delivery.state;
}

function stateTimeMs(delivery: DeliveryDoc, state: DeliveryState): number | null {
  if (state === "read" && delivery.readTs) return delivery.readTs * 1000;
  if ((state === "shown" || state === "read") && delivery.shownTs) return delivery.shownTs * 1000;
  if (state === "sent" && delivery.sentTs) return delivery.sentTs * 1000;
  return null;
}

const WHERE: Record<string, string> = {
  pager: "pager",
  webapp: "app",
  gchat: "Google Chat",
  sms: "SMS",
  bridge: "Google Chat",
  fed: "other family",
};

// Plainer words for stored `DeliveryDoc.error` reasons; others show as written.
const ERROR_TEXT: Record<string, string> = {
  unpeered: "no longer linked",
  no_peer: "not linked",
  fed_off: "Federation is off for this family",
  fed_reauth: "This family link needs re-authorizing",
  peer_offline: "family offline",
  bridge_offline: "bridge phone offline",
};

const LABELS: Record<string, Partial<Record<DeliveryState, string>>> = {
  pager: { queued: "waiting for pager", sent: "sent to pager", shown: "on pager", read: "read on pager" },
  webapp: { queued: "waiting", sent: "sent to app", shown: "shown in app", read: "read in app" },
  gchat: { queued: "waiting", sent: "sent to Google Chat", shown: "sent to Google Chat", read: "read in Google Chat" },
  bridge: {
    queued: "waiting for the phone",
    sent: "sent on Google Chat",
    shown: "sent on Google Chat",
    read: "sent on Google Chat",
  },
  sms: { queued: "waiting to send SMS", sent: "sent by SMS", shown: "sent by SMS", read: "sent by SMS" },
};

/** Chip text saying where a delivery went; unknown kinds fall back to `kind: state`. */
export function chipLabel(kind: string, state: DeliveryState, opts: { server?: string; error?: string | null } = {}): string {
  const where = WHERE[kind];
  if (!where) return `${kind}: ${state}`;
  if (kind === "fed") {
    const server = opts.server ?? "other family";
    if (state === "queued") return `waiting for ${server}`;
    if (state === "sent" || state === "shown" || state === "read") return `sent to ${server}`;
    if (state === "failed") return opts.error ? `failed: ${ERROR_TEXT[opts.error] ?? opts.error}` : "failed";
  }
  if (state === "failed") return opts.error ? `${where} failed: ${ERROR_TEXT[opts.error] ?? opts.error}` : `${where} failed`;
  if (state === "expired") return "expired";
  if (state === "fulfilled") return "location received";
  return LABELS[kind]?.[state] ?? `${kind}: ${state}`;
}

/** Per-backend delivery-state chips read from the message's embedded `deliveries` map. */
export default function DeliveryChips({ message }: { message: MessageDoc }) {
  const entries = Object.entries(message.deliveries);
  if (entries.length === 0) return null;

  return (
    <Stack direction="row" spacing={0.5} useFlexGap sx={{ mt: 0.5, flexWrap: "wrap" }}>
      {entries.map(([bid, delivery]) => {
        const state = effectiveState(message, delivery);
        const atMs = stateTimeMs(delivery, state);
        const fed = delivery.kind === "fed" ? fedDeliveryLabel(delivery) : null;
        const label = `${chipLabel(delivery.kind, state, { server: fed?.name, error: delivery.error })}${atMs ? " " + formatClock(atMs) : ""}`;
        return (
          <Chip
            key={bid}
            size="small"
            label={label}
            title={fed?.host}
            color={STATE_COLOR[state]}
            variant={state === "queued" ? "outlined" : "filled"}
          />
        );
      })}
    </Stack>
  );
}
