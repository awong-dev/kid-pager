"use client";

/** "Other families" in the Outside contacts section of People & contacts: federation identity, link invites and the list of links. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";

import FedAddContactDialog from "@/components/FedAddContactDialog";
import {
  acceptFedInvite,
  deleteFedLink,
  fedLinkElementId,
  fedErrorText,
  getFamilyFed,
  linkStateLabel,
  newFedInvite,
  parseFedInviteHash,
  parseFedLinkHash,
  putFedShared,
  reauthConfirmDetail,
  reauthFedLink,
  type FedReauthInvite,
  type ReauthConfirm,
  resumeFedLink,
  shortFingerprint,
  suspendFedLink,
  type FedLink,
  type FedThisFamily,
} from "@/lib/federation";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

export interface FedShareMember {
  uid: string;
  alias: string;
  displayName: string;
}

function CopyButton({ text, label }: { text: string; label: string }) {
  return (
    <IconButton size="small" aria-label={label} onClick={() => void navigator.clipboard?.writeText(text)}>
      <ContentCopyIcon fontSize="small" />
    </IconButton>
  );
}

function expiryText(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function stateColor(s: ReturnType<typeof linkStateLabel>): "success" | "default" | "error" | "warning" {
  return s === "active" ? "success" : s === "offline" ? "error" : s === "reauth" ? "warning" : "default";
}

export default function FedLinksSection({
  members,
  federation,
}: {
  members: FedShareMember[];
  /** `GET /api/family`'s effective flag; anything but `true` renders nothing. */
  federation?: boolean;
}) {
  const fullScreen = useFullScreenDialog();
  const [data, setData] = useState<FedThisFamily | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [invite, setInvite] = useState<{ blob: string; expiresAt: number } | null>(null);
  const [enterOpen, setEnterOpen] = useState(false);
  const [enterText, setEnterText] = useState("");
  const [enterError, setEnterError] = useState<string | null>(null);
  const [linked, setLinked] = useState<FedLink | null>(null);
  const [removing, setRemoving] = useState<FedLink | null>(null);
  const [addFor, setAddFor] = useState<FedLink | null>(null);
  const [confirmReauth, setConfirmReauth] = useState<ReauthConfirm | null>(null);
  const [reauthOut, setReauthOut] = useState<{ link: FedLink; res: FedReauthInvite } | null>(null);

  // `/family/people#fedInvite=<invite>`: open Enter invite prefilled, then drop
  // the fragment so the (single-use) invite does not linger in the address bar.
  useEffect(() => {
    if (federation !== true) return;
    const inv = parseFedInviteHash(window.location.hash);
    if (!inv) return;
    const rest = window.location.hash
      .slice(1)
      .split("&")
      .filter((p) => !p.startsWith("fedInvite="))
      .join("&");
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${rest ? `#${rest}` : ""}`);
    const t = setTimeout(() => {
      setEnterText(inv);
      setEnterError(null);
      setEnterOpen(true);
    }, 0);
    return () => clearTimeout(t);
  }, [federation]);

  // `/family/people#federation&link=<id>` (the alerts' "Review link"): once the
  // links are loaded, scroll to that row and highlight it.
  const [highlight, setHighlight] = useState<string | null>(null);
  const hasData = data !== null;
  useEffect(() => {
    if (!hasData) return;
    const id = parseFedLinkHash(window.location.hash);
    if (!id) return;
    const t = setTimeout(() => {
      setHighlight(id);
      (document.getElementById(fedLinkElementId(id)) ?? document.getElementById("federation"))?.scrollIntoView({ block: "start" });
    }, 0);
    return () => clearTimeout(t);
  }, [hasData]);

  const load = useCallback(async () => {
    if (federation !== true) return;
    try {
      setData(await getFamilyFed());
      setError(null);
    } catch (e) {
      setError(fedErrorText(e, "Could not load federation"));
    }
  }, [federation]);

  useEffect(() => {
    const first = setTimeout(() => void load(), 0);
    const poll = setInterval(() => void load(), 20_000);
    return () => {
      clearTimeout(first);
      clearInterval(poll);
    };
  }, [load]);

  function replace(l: FedLink) {
    setData((d) => (d ? { ...d, links: d.links.map((x) => (x.id === l.id ? l : x)) } : d));
  }

  async function run(fn: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(fedErrorText(e, fallback));
    } finally {
      setBusy(false);
    }
  }

  async function makeInvite() {
    await run(async () => {
      const res = await newFedInvite();
      setInvite({ blob: res.invite, expiresAt: res.expiresAt });
    }, "Could not create an invite");
  }

  async function enter(confirmed = false) {
    setBusy(true);
    setEnterError(null);
    try {
      const l = await acceptFedInvite(enterText.trim(), confirmed);
      setEnterOpen(false);
      setConfirmReauth(null);
      setEnterText("");
      setLinked(l);
      await load();
    } catch (e) {
      const c = reauthConfirmDetail(e);
      if (c) setConfirmReauth(c);
      else {
        setConfirmReauth(null);
        setEnterError(fedErrorText(e, "Could not use that invite"));
      }
    } finally {
      setBusy(false);
    }
  }

  async function toggleShared(l: FedLink, uid: string, on: boolean) {
    const next = on ? [...l.shared, uid] : l.shared.filter((u) => u !== uid);
    await run(async () => replace(await putFedShared(l.id, next)), "Could not save sharing");
  }

  if (federation !== true) return null;
  const links = data?.links ?? [];

  return (
    <Stack spacing={2} id="federation">
      <Typography variant="h6">Other families</Typography>
      {error && <Alert severity="error">{error}</Alert>}

      {data && (
        <Stack spacing={0.25}>
          <Typography variant="subtitle1">This family</Typography>
          <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
            <Typography variant="body2">
              {data.name} &middot; <span style={{ fontFamily: "monospace" }}>{shortFingerprint(data.fingerprint)}</span>
            </Typography>
            <CopyButton text={data.fingerprint} label="copy fingerprint" />
          </Stack>
          <Typography variant="caption" color="text.secondary">
            Read it to the other family&apos;s admin to check you linked the right family.
          </Typography>
        </Stack>
      )}

      <Stack spacing={1}>
        <Typography variant="subtitle1">Link a family</Typography>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
          <Button variant="contained" disabled={busy} onClick={() => void makeInvite()}>
            Create invite
          </Button>
          <Button
            variant="outlined"
            onClick={() => {
              setEnterError(null);
              setEnterOpen(true);
            }}
          >
            Enter invite
          </Button>
        </Stack>
        {invite && (
          <Alert severity="success" onClose={() => setInvite(null)}>
            <Typography variant="body2" sx={{ mb: 0.5 }}>
              Send this to the other family&apos;s admin. It works once and expires in 24 hours (
              {expiryText(invite.expiresAt)}). Anyone who has it can link to your family, so send it privately.
            </Typography>
            <Stack direction="row" sx={{ alignItems: "center" }}>
              <Typography variant="body2" sx={{ fontFamily: "monospace", overflowWrap: "anywhere", flexGrow: 1 }}>
                {invite.blob}
              </Typography>
              <CopyButton text={invite.blob} label="copy invite" />
            </Stack>
          </Alert>
        )}
        {linked && (
          <Alert severity="success" onClose={() => setLinked(null)}>
            <Typography variant="body2">
              Linked to {linked.remote.name} ({linked.remote.host}).
            </Typography>
            <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
              Their key: <span style={{ fontFamily: "monospace" }}>{linked.remote.fingerprint}</span>
            </Typography>
            {data && (
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                Yours: <span style={{ fontFamily: "monospace" }}>{data.fingerprint}</span>
              </Typography>
            )}
            <Typography variant="body2" sx={{ mt: 0.5 }}>
              Check these with the other admin by phone or in person.
            </Typography>
          </Alert>
        )}
      </Stack>

      {links.length > 0 && <Typography variant="subtitle1">Linked families</Typography>}
      {links.map((l) => {
        const st = linkStateLabel(l);
        const reauth = st === "reauth";
        return (
          <Stack
            key={l.id}
            id={fedLinkElementId(l.id)}
            spacing={0.5}
            sx={{ borderTop: 1, borderColor: "divider", pt: 1, scrollMarginTop: 80, ...(highlight === l.id ? { bgcolor: "action.hover" } : {}) }}
          >
            <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
              <Typography variant="body1">{l.remote.name}</Typography>
              <Chip size="small" variant="outlined" color={stateColor(st)} label={reauth ? "Needs re-authorizing" : st} />
              {l.pending > 0 && <Chip size="small" variant="outlined" label={`${l.pending} queued`} />}
              {l.announcePending && <Chip size="small" variant="outlined" label="moving..." />}
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ overflowWrap: "anywhere" }}>
              {l.remote.host} &middot; {shortFingerprint(l.remote.fingerprint)}
            </Typography>
            {reauth && (
              <Typography variant="body2">
                This link was restored from a backup. {l.remote.name}&apos;s admin has to confirm it before messages
                flow again.
              </Typography>
            )}
            {!reauth && l.remoteKeyChanged && (
              <Alert severity="warning">
                {l.remote.name}&apos;s key changed since your backup. Check the fingerprint with their admin.
              </Alert>
            )}
            <Typography variant="body2" sx={{ pt: 0.5 }}>
              Shared with {l.remote.name}
            </Typography>
            <Stack direction="row" useFlexGap sx={{ flexWrap: "wrap", columnGap: 2 }}>
              {members.map((m) => (
                <FormControlLabel
                  key={m.uid}
                  control={
                    <Checkbox
                      size="small"
                      checked={l.shared.includes(m.uid)}
                      disabled={busy || reauth}
                      onChange={(e) => void toggleShared(l, m.uid, e.target.checked)}
                    />
                  }
                  label={m.displayName}
                />
              ))}
            </Stack>
            <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
              {reauth && (
                <Button
                  size="small"
                  variant="contained"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => setReauthOut({ link: l, res: await reauthFedLink(l.id) }), "Could not create a re-authorize invite")
                  }
                >
                  Re-authorize
                </Button>
              )}
              {!reauth && (
                <Button size="small" disabled={l.state !== "active"} onClick={() => setAddFor(l)}>
                  Add people from {l.remote.name}
                </Button>
              )}
              {reauth ? null : l.state === "suspended" ? (
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => void run(async () => replace(await resumeFedLink(l.id)), "Could not resume")}
                >
                  Resume
                </Button>
              ) : (
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => void run(async () => replace(await suspendFedLink(l.id)), "Could not suspend")}
                >
                  Suspend
                </Button>
              )}
              <Button size="small" color="error" disabled={busy} onClick={() => setRemoving(l)}>
                Remove
              </Button>
            </Stack>
          </Stack>
        );
      })}

      <Dialog
        fullScreen={fullScreen}
        open={enterOpen && confirmReauth !== null}
        onClose={() => (busy ? undefined : setConfirmReauth(null))}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>Re-authorize the link with {confirmReauth?.link.remote.name}?</DialogTitle>
        <DialogContent>
          {confirmReauth && (
            <Stack spacing={1.5} sx={{ mt: 1 }}>
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                Server: {confirmReauth.link.remote.host} &rarr; {confirmReauth.newHost}
                {confirmReauth.newPort ? `:${confirmReauth.newPort}` : ""}
              </Typography>
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                Old fingerprint:{" "}
                <span style={{ fontFamily: "monospace" }}>{confirmReauth.link.remote.fingerprint}</span>
              </Typography>
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                New fingerprint: <span style={{ fontFamily: "monospace" }}>{confirmReauth.newFingerprint}</span>
              </Typography>
              <Alert severity="warning">
                Only confirm if {confirmReauth.link.remote.name}&apos;s admin told you they restored from a backup.
                Check the new fingerprint with them by phone or in person. After this, the old server can no longer use
                this link.
              </Alert>
              {enterError && <Alert severity="error">{enterError}</Alert>}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => setConfirmReauth(null)}>
            Cancel
          </Button>
          <Button variant="contained" disabled={busy} onClick={() => void enter(true)}>
            Re-authorize
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog
        fullScreen={fullScreen}
        open={reauthOut !== null}
        onClose={() => setReauthOut(null)}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>Re-authorize {reauthOut?.link.remote.name}</DialogTitle>
        <DialogContent>
          {reauthOut && (
            <Stack spacing={1.5} sx={{ mt: 1 }}>
              {reauthOut.res.url && (
                <Stack spacing={0.5}>
                  <Typography variant="body2">
                    Send this link to {reauthOut.link.remote.name}&apos;s admin. It works once and expires in 24 hours (
                    {expiryText(reauthOut.res.expiresAt)}).
                  </Typography>
                  <Stack direction="row" sx={{ alignItems: "center" }}>
                    <Typography variant="body2" sx={{ fontFamily: "monospace", overflowWrap: "anywhere", flexGrow: 1 }}>
                      {reauthOut.res.url}
                    </Typography>
                    <CopyButton text={reauthOut.res.url} label="copy link" />
                  </Stack>
                </Stack>
              )}
              <Stack spacing={0.5}>
                <Typography variant="body2">
                  {reauthOut.res.url ? "Or have them paste this under Enter invite:" : "Have them paste this under Enter invite. It works once and expires in 24 hours:"}
                </Typography>
                <Stack direction="row" sx={{ alignItems: "center" }}>
                  <Typography variant="body2" sx={{ fontFamily: "monospace", overflowWrap: "anywhere", flexGrow: 1 }}>
                    {reauthOut.res.invite}
                  </Typography>
                  <CopyButton text={reauthOut.res.invite} label="copy invite" />
                </Stack>
              </Stack>
              {data && (
                <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                  This family&apos;s new fingerprint, to read to them by phone:{" "}
                  <span style={{ fontFamily: "monospace" }}>{data.fingerprint}</span>
                </Typography>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReauthOut(null)}>Done</Button>
        </DialogActions>
      </Dialog>

      <Dialog
        fullScreen={fullScreen}
        open={enterOpen && confirmReauth === null}
        onClose={() => setEnterOpen(false)}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>Enter invite</DialogTitle>
        <DialogContent>
          {enterError && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {enterError}
            </Alert>
          )}
          <TextField
            autoFocus
            fullWidth
            margin="dense"
            multiline
            minRows={3}
            label="Invite"
            value={enterText}
            onChange={(e) => setEnterText(e.target.value)}
            helperText="Paste the invite the other family's admin sent you."
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEnterOpen(false)}>Cancel</Button>
          <Button variant="contained" disabled={busy || !enterText.trim()} onClick={() => void enter(false)}>
            Link
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog fullScreen={fullScreen} open={removing !== null} onClose={() => (busy ? undefined : setRemoving(null))}>
        <DialogTitle>Remove {removing?.remote.name}?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Messages to and from {removing?.remote.name} stop and their contacts become unusable.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRemoving(null)} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="contained"
            color="error"
            disabled={busy}
            onClick={() => {
              const l = removing;
              if (!l) return;
              void run(async () => {
                await deleteFedLink(l.id);
                setRemoving(null);
                await load();
              }, "Could not remove the link");
            }}
          >
            Remove
          </Button>
        </DialogActions>
      </Dialog>

      {addFor && (
        <FedAddContactDialog
          open
          links={links}
          initialLinkId={addFor.id}
          members={members}
          onClose={() => setAddFor(null)}
          onAdded={() => void load()}
        />
      )}
    </Stack>
  );
}
