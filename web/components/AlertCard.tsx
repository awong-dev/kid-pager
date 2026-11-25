"use client";

/** One alert row; actions call `POST /api/family/alerts/{id}/{approve|block|dismiss}`. */

import { useState } from "react";
import Collapse from "@mui/material/Collapse";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardActions from "@mui/material/CardActions";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutlined";
import ForumIcon from "@mui/icons-material/Forum";
import PersonAddIcon from "@mui/icons-material/PersonAdd";
import ReportIcon from "@mui/icons-material/Report";
import SmsIcon from "@mui/icons-material/Sms";
import SettingsInputAntennaIcon from "@mui/icons-material/SettingsInputAntenna";
import PublicIcon from "@mui/icons-material/Public";

import { Link } from "@/lib/router";
import BridgeTopups from "@/components/BridgeTopups";
import ChatSubscribeDialog from "@/components/ChatSubscribeDialog";
import { formatPhoneDigits } from "@/components/NewChatDialog";
import { ApiError, api } from "@/lib/api";
import { ignoreChat, sourceLabel } from "@/lib/bridges";
import { isHashAlias } from "@/lib/names";
import { fedMovedInfo, fedReauthInfo } from "@/lib/federation";
import { familyQuery } from "@/lib/family-context";
import type { AlertDoc } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

/** One row of `GET /api/family/alerts/{id}/held`. */
interface HeldText {
  id: string;
  body: string;
  // epoch seconds, epoch ms or an ISO string
  receivedAt: number | string | null;
  status: "held" | "delivered" | "too_long" | "blocked" | "dismissed" | "empty";
}

interface ApproveResult {
  delivered?: number;
  undelivered?: number;
}

function formatReceived(v: HeldText["receivedAt"]): string {
  if (v === null || v === undefined) return "";
  const ms = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(v);
  return Number.isNaN(ms) ? "" : new Date(ms).toLocaleString();
}

export interface AlertRow extends AlertDoc {
  id: string;
}

function formatAge(ts: AlertDoc["ts"]): string {
  if (!ts) return "just now";
  const ms = Date.now() - ts.toDate().getTime();
  if (ms < 60_000) return "just now";
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function kindIcon(kind: AlertDoc["kind"]) {
  switch (kind) {
    case "sms_unknown":
      return <SmsIcon fontSize="small" />;
    case "new_conversation":
      return <ChatBubbleOutlineIcon fontSize="small" />;
    case "contact_request":
      return <PersonAddIcon fontSize="small" />;
    case "chat_unknown":
      return <ForumIcon fontSize="small" />;
    case "hook_flag":
    case "hook_down":
      return <ReportIcon fontSize="small" />;
    case "system":
      return <SettingsInputAntennaIcon fontSize="small" />;
    case "fed_contact":
    case "fed_unknown":
    case "fed_moved":
    case "fed_reauth":
      return <PublicIcon fontSize="small" />;
  }
}

const SYSTEM_TITLES: Record<string, string> = {
  bridge_offline: "Bridge phone offline",
  bridge_online: "Bridge phone restored",
  bridge_degraded: "Bridge phone degraded",
  bridge_battery: "Bridge phone battery low",
  sms_low: "Texts running low",
  sms_out: "Out of texts",
  sms_topped_up: "Texts topped up",
  peer_down: "Server unreachable",
  peer_up: "Server restored",
  link_unreachable: "Chat link unreachable",
  family_frozen: "Family frozen",
};

// A notice that reports a problem going away: shown as resolved history.
const RESTORED_CODES = new Set(["bridge_online", "peer_up", "sms_topped_up"]);

function isRestored(alert: Pick<AlertDoc, "code">): boolean {
  return RESTORED_CODES.has(alert.code ?? "");
}

function systemTitle(code: string | undefined): string {
  return (code && SYSTEM_TITLES[code]) || "System notice";
}

function kindLabel(kind: AlertDoc["kind"], source?: AlertDoc["source"]): string {
  switch (kind) {
    case "sms_unknown":
      return "Unknown number";
    case "new_conversation":
      return "New conversation";
    case "contact_request":
      return "Contact request";
    case "chat_unknown":
      // name the app the bridged conversation came from
      return source === "gchat" || source === "gvoice" || source === "whatsapp"
        ? sourceLabel(source)
        : "Unknown source";
    case "hook_flag":
      return "Flagged message";
    case "hook_down":
      return "Hook failing";
    case "system":
      return "System";
    case "fed_contact":
      return "Contact on another server";
    case "fed_unknown":
      return "Unknown person on another server";
    case "fed_moved":
      return "Linked family moved";
    case "fed_reauth":
      return "Linked family needs re-authorizing";
  }
}

/** Who a `new_conversation` alert's kid wrote to: never a hash alias. */
function newConversationPeer(alert: AlertRow): string {
  const phone = alert.peerPhone
    ? formatPhoneDigits(alert.peerPhone.replace(/^\+/, ""))
    : null;
  const name = alert.peerName?.trim();
  if (phone) {
    return name ? `${name} (${phone})` : phone;
  }
  if (alert.peerAlias) {
    const at = isHashAlias(alert.peerAlias) ? null : `@${alert.peerAlias}`;
    if (at) return name ? `${at} (${name})` : at;
    return name || "someone";
  }
  return name || "someone";
}

export default function AlertCard({ alert }: { alert: AlertRow }) {
  const fullScreen = useFullScreenDialog();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // sms_unknown approve dialog.
  const [smsOpen, setSmsOpen] = useState(false);
  const [smsName, setSmsName] = useState("");

  // 409 name collision on a contact_request approve: ask for another name.
  const [nameOpen, setNameOpen] = useState(false);
  const [nameValue, setNameValue] = useState("");
  const [nameConflict, setNameConflict] = useState<string | null>(null);

  // held-text expander and approve outcome (`sms_unknown`)
  const [heldOpen, setHeldOpen] = useState(false);
  const [heldList, setHeldList] = useState<HeldText[] | null>(null);
  const [heldError, setHeldError] = useState<string | null>(null);
  const [result, setResult] = useState<ApproveResult | null>(null);

  const heldCount = alert.kind === "sms_unknown" ? (alert.heldCount ?? 0) : 0;
  const hasHeld = heldCount > 0;

  async function toggleHeld() {
    const next = !heldOpen;
    setHeldOpen(next);
    if (!next) return;
    setHeldError(null);
    try {
      const resp = await api.get<{ held: HeldText[] }>(`/family/alerts/${alert.id}/held${familyQuery()}`);
      setHeldList(resp.held);
    } catch (err) {
      setHeldError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to load messages");
    }
  }

  const handled = alert.status !== "open";

  // chat_unknown: Subscribe opens the dialog, whose endpoint decides the alert
  const [subscribeOpen, setSubscribeOpen] = useState(false);
  const [chatResult, setChatResult] = useState<string | null>(null);
  const chatPeople = alert.people ?? [];
  const chatWaiting = alert.heldCount ?? 0;
  const chatReady = Boolean(alert.bridgeId && alert.convRef);

  async function ignoreChatAlert() {
    if (!alert.bridgeId || !alert.convRef) return;
    setSubmitting(true);
    setError(null);
    try {
      await ignoreChat(alert.bridgeId, alert.convRef);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to ignore");
    } finally {
      setSubmitting(false);
    }
  }

  async function post(action: "approve" | "block" | "dismiss", body: unknown) {
    setSubmitting(true);
    setError(null);
    try {
      const resp = await api.post<ApproveResult | null>(`/family/alerts/${alert.id}/${action}${familyQuery()}`, body);
      if (action === "approve" && alert.kind === "sms_unknown" && hasHeld) setResult(resp ?? {});
      setSmsOpen(false);
      setNameOpen(false);
      setNameConflict(null);
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.detail ?? err.message) : `Failed to ${action}`;
      if (err instanceof ApiError && err.status === 409 && action === "approve") {
        if (alert.kind === "fed_contact" && detail.startsWith("not_shared")) {
          setError(
            `@${alert.subjectAlias} is not shared with ${alert.fedName ?? "that family"} yet. Share them under People > Other families, then approve.`
          );
        } else if (alert.kind === "fed_contact" && detail.startsWith("alias_taken")) {
          setError("That alias is taken here. Add them from Contacts > Add from another family and choose another alias.");
        } else if (alert.kind === "contact_request" || (alert.kind === "fed_contact" && detail.startsWith("name_taken"))) {
          setNameConflict(detail);
          setNameValue((body as { name?: string }).name ?? alert.peerName ?? alert.preview);
          setNameOpen(true);
        } else if (alert.kind === "sms_unknown") {
          setNameConflict(detail);
        } else {
          setError(detail);
        }
      } else {
        setError(detail);
      }
    } finally {
      setSubmitting(false);
    }
  }

  function openSmsApprove() {
    setError(null);
    setSmsName("");
    setNameConflict(null);
    setSmsOpen(true);
  }

  const isSms = alert.peerPhone != null;

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center", mb: 1 }}>
          <Chip size="small" icon={kindIcon(alert.kind)} label={kindLabel(alert.kind, alert.source)} />
          <Typography variant="caption" color="text.secondary">
            {formatAge(alert.ts)}
          </Typography>
          {handled && (
            <Chip
              size="small"
              variant="outlined"
              color={alert.status === "dismissed" ? "default" : "success"}
              label={alert.kind === "system" ? (isRestored(alert) ? "restored" : "resolved") : alert.status}
            />
          )}
        </Stack>

        {alert.kind === "sms_unknown" && (
          <>
            <Typography variant="body1">
              {hasHeld
                ? `to @${alert.subjectAlias} · ${formatPhoneDigits((alert.peerPhone ?? "").replace(/^\+/, ""))}`
                : `${alert.peerPhone ?? "Unknown number"} → @${alert.subjectAlias}`}
            </Typography>
            {alert.preview && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                &quot;{alert.preview}&quot;
              </Typography>
            )}
            {hasHeld && (
              <>
                <Button size="small" onClick={() => void toggleHeld()} sx={{ mt: 0.5, px: 0 }}>
                  {heldCount} {heldCount === 1 ? "message" : "messages"} waiting {heldOpen ? "▴" : "▾"}
                </Button>
                <Collapse in={heldOpen} unmountOnExit>
                  {heldError && <Alert severity="error">{heldError}</Alert>}
                  {heldList === null && !heldError && (
                    <Typography variant="caption" color="text.secondary">
                      Loading…
                    </Typography>
                  )}
                  <Stack spacing={1} sx={{ mt: 0.5 }}>
                    {heldList?.map((h) => (
                      <div key={h.id}>
                        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
                          <Typography variant="caption" color="text.secondary">
                            {formatReceived(h.receivedAt)}
                          </Typography>
                          {h.status !== "held" && (
                            <Chip size="small" variant="outlined" label={h.status === "empty" ? "empty message" : h.status.replace("_", " ")} />
                          )}
                        </Stack>
                        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                          {h.body}
                        </Typography>
                      </div>
                    ))}
                  </Stack>
                </Collapse>
              </>
            )}
            {hasHeld && !handled && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                Block: no more texts from this number reach @{alert.subjectAlias}; the waiting messages are
                discarded. Dismiss: the waiting messages are discarded.
              </Typography>
            )}
            {result && (
              <Alert severity={result.undelivered ? "warning" : "success"} sx={{ mt: 1 }}>
                Delivered {result.delivered ?? 0}
                {result.undelivered ? `; ${result.undelivered} could not be delivered` : ""}
              </Alert>
            )}
          </>
        )}

        {alert.kind === "new_conversation" && (
          <>
            <Typography variant="body1">
              @{alert.subjectAlias} started a chat with {newConversationPeer(alert)}
            </Typography>
            {alert.preview && !/^@\S+ started a chat with /.test(alert.preview) && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                &ldquo;{alert.preview}&rdquo;
              </Typography>
            )}
          </>
        )}

        {(alert.kind === "hook_flag" || alert.kind === "hook_down") && (
          <>
            <Typography variant="body1">
              {alert.title || (alert.kind === "hook_flag" ? "A message was flagged" : `Hook ${alert.hookName ?? ""} is failing`)}
            </Typography>
            {alert.preview && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                {alert.preview}
              </Typography>
            )}
            {alert.hookName && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                Hook: {alert.hookName}
              </Typography>
            )}
          </>
        )}

        {alert.kind === "system" && (
          <>
            <Typography variant="body1" sx={{ fontWeight: 600 }} color={isRestored(alert) || handled ? "text.secondary" : undefined}>
              {systemTitle(alert.code)}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
              {alert.preview}
            </Typography>
            {!handled && (alert.code === "sms_low" || alert.code === "sms_out") && alert.key && (
              <Stack sx={{ mt: 1 }}>
                <BridgeTopups bridgeId={alert.key} compact />
              </Stack>
            )}
          </>
        )}

        {(alert.kind === "fed_contact" || alert.kind === "fed_unknown") && (
          <>
            <Typography variant="body1">{alert.preview}</Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
              {alert.kind === "fed_contact"
                ? `Approve: ${alert.peerName ?? "they"} become a contact of @${alert.subjectAlias}, so they can write back.`
                : "Their message was not delivered. Add them as a contact to let them through."}
            </Typography>
          </>
        )}

        {alert.kind === "fed_moved" && <Typography variant="body1">{fedMovedInfo(alert).text}</Typography>}
        {alert.kind === "fed_reauth" && <Typography variant="body1">{fedReauthInfo(alert).text}</Typography>}

        {alert.kind === "contact_request" && (
          <>
            {isSms ? (
              <>
                <Typography variant="body1">
                  @{alert.subjectAlias} wants to text {alert.peerName ?? "a number"} (
                  {formatPhoneDigits((alert.peerPhone ?? "").replace(/^\+/, ""))}) · added from the pager
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                  Approve: @{alert.subjectAlias}&apos;s pager can text this number.
                </Typography>
              </>
            ) : (
              <>
                <Typography variant="body1">
                  @{alert.subjectAlias} wants to message @{alert.peerAlias} ({alert.peerName ?? "unknown"}) · added
                  from the pager
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                  Approve: @{alert.subjectAlias} can message @{alert.peerAlias}.
                </Typography>
              </>
            )}
          </>
        )}

        {alert.kind === "chat_unknown" && (
          <>
            <Typography variant="body1">
              {alert.convTitle || "Untitled conversation"} → @{alert.subjectAlias}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {alert.isGroup ? `Group (${chatPeople.length} ${chatPeople.length === 1 ? "person" : "people"} seen)` : "Direct message"}
              {alert.source === "gvoice" || alert.source === "whatsapp" ? ` · ${sourceLabel(alert.source)}` : ""}
            </Typography>
            {chatPeople.length > 0 && (
              <Stack direction="row" spacing={0.5} useFlexGap sx={{ mt: 0.5, flexWrap: "wrap" }}>
                {chatPeople.map((p) => (
                  <Chip key={p} size="small" variant="outlined" label={p} />
                ))}
              </Stack>
            )}
            {alert.preview && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                &quot;{alert.preview}&quot;
              </Typography>
            )}
            {chatWaiting > 0 && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                {chatWaiting} {chatWaiting === 1 ? "message" : "messages"} waiting
              </Typography>
            )}
            {chatResult && (
              <Alert severity="success" sx={{ mt: 1 }}>
                {chatResult}
              </Alert>
            )}
          </>
        )}

        {error && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {error}
          </Alert>
        )}
      </CardContent>

      {!handled && (
        <CardActions>
          {alert.kind === "sms_unknown" && (
            <Button size="small" variant="contained" onClick={openSmsApprove} disabled={submitting}>
              Approve
            </Button>
          )}
          {alert.kind === "new_conversation" && (
            <>
              {alert.convKey && (
                <Button
                  size="small"
                  component={Link}
                  href={`/chat/view/${encodeURIComponent(alert.convKey)}`}
                >
                  View
                </Button>
              )}
              <Button
                size="small"
                variant="contained"
                onClick={() => void post("approve", {})}
                disabled={submitting}
              >
                Approve
              </Button>
            </>
          )}
          {alert.kind === "hook_flag" && alert.convKey && (
            <Button size="small" component={Link} href={`/chat/view/${encodeURIComponent(alert.convKey)}`}>
              View conversation
            </Button>
          )}
          {(alert.kind === "contact_request" || alert.kind === "fed_contact") && (
            <Button size="small" variant="contained" onClick={() => void post("approve", {})} disabled={submitting}>
              Approve
            </Button>
          )}
          {alert.kind === "fed_unknown" && (
            <Button size="small" component={Link} href="/family/people#contacts">
              Outside contacts
            </Button>
          )}
          {(alert.kind === "fed_moved" || alert.kind === "fed_reauth") && (
            <Button
              size="small"
              variant="contained"
              component={Link}
              href={`/family/people#federation${alert.fedLinkId ? `&link=${encodeURIComponent(alert.fedLinkId)}` : ""}`}
            >
              Review link
            </Button>
          )}
          {alert.kind === "chat_unknown" && (
            <>
              <Button size="small" variant="contained" disabled={submitting || !chatReady} onClick={() => setSubscribeOpen(true)}>
                Subscribe
              </Button>
              <Button size="small" disabled={submitting || !chatReady} onClick={() => void ignoreChatAlert()}>
                Ignore
              </Button>
            </>
          )}
          {alert.kind !== "chat_unknown" && alert.kind !== "hook_flag" && alert.kind !== "hook_down" && alert.kind !== "system" && alert.kind !== "fed_moved" && alert.kind !== "fed_reauth" && alert.kind !== "fed_contact" && alert.kind !== "fed_unknown" && (alert.kind !== "contact_request" || isSms) && (
            <Button size="small" color="warning" onClick={() => void post("block", {})} disabled={submitting}>
              Block
            </Button>
          )}
          <Button size="small" onClick={() => void post("dismiss", {})} disabled={submitting}>
            Dismiss
          </Button>
        </CardActions>
      )}

      {alert.kind === "chat_unknown" && alert.bridgeId && alert.convRef && (
        <ChatSubscribeDialog
          ownerUid={alert.subjectUid}
          target={
            subscribeOpen
              ? {
                  bridgeId: alert.bridgeId,
                  ref: alert.convRef,
                  title: alert.convTitle ?? "",
                  isGroup: alert.isGroup ?? false,
                  people: chatPeople,
                }
              : null
          }
          onClose={() => setSubscribeOpen(false)}
          onSubscribed={(res, name) => {
            setSubscribeOpen(false);
            setChatResult(`${name} is on the pager; ${res.delivered} waiting messages delivered`);
          }}
        />
      )}

      <Dialog fullScreen={fullScreen} open={smsOpen} onClose={() => setSmsOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Approve {alert.peerPhone ?? "number"}</DialogTitle>
        <DialogContent>
          {nameConflict && smsOpen && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {nameConflict}
            </Alert>
          )}
          <TextField
            autoFocus
            label="Name"
            value={smsName}
            onChange={(e) => setSmsName(e.target.value)}
            fullWidth
            sx={{ mt: 1 }}
            helperText={
              hasHeld
                ? `@${alert.subjectAlias} can text this number from their pager, and the ${heldCount} waiting ${heldCount === 1 ? "message is" : "messages are"} delivered now.`
                : `Adds it to @${alert.subjectAlias}'s pager SMS list.`
            }
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSmsOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={submitting || smsName.trim().length === 0}
            onClick={() => void post("approve", { name: smsName.trim(), forAlias: alert.subjectAlias })}
          >
            Approve
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog fullScreen={fullScreen} open={nameOpen} onClose={() => setNameOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Choose another name</DialogTitle>
        <DialogContent>
          {nameConflict && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {nameConflict}
            </Alert>
          )}
          <TextField
            autoFocus
            label="Name"
            value={nameValue}
            onChange={(e) => setNameValue(e.target.value)}
            fullWidth
            sx={{ mt: 1 }}
            helperText="Must be unique in the family; the pager shows the first 16 characters."
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setNameOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={submitting || nameValue.trim().length === 0}
            onClick={() => void post("approve", { name: nameValue.trim() })}
          >
            Approve
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
