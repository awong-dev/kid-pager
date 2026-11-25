"use client";

/** `/admin/federation` (super only): server and per-family switches, and a table of family links
 * with Suspend / Resume / Remove. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import {
  deleteAdminFedLink,
  getFedState,
  linkStateLabel,
  listAdminFedLinks,
  resumeAdminFedLink,
  setFedEnabled,
  setFedFamily,
  suspendAdminFedLink,
  type FedAdminLink,
  type FedState,
} from "@/lib/federation";
import { responsiveTableSx } from "@/lib/tableSx";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? String(e.detail ?? e.message) : fallback;
}

type Action = "suspend" | "resume" | "delete";

function FederationInner() {
  const fullScreen = useFullScreenDialog();
  const [state, setState] = useState<FedState | null>(null);
  const [links, setLinks] = useState<FedAdminLink[]>([]);
  const [offConfirm, setOffConfirm] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ link: FedAdminLink; action: Action } | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const st = await getFedState();
      setState(st);
      setLinks(st.enabled ? await listAdminFedLinks() : []);
      setError(null);
    } catch (e) {
      setError(errText(e, "Could not load federation"));
    }
  }, []);

  useEffect(() => {
    const first = setTimeout(() => void load(), 0);
    const poll = setInterval(() => void load(), 15_000);
    return () => {
      clearTimeout(first);
      clearInterval(poll);
    };
  }, [load]);

  async function runConfirm() {
    if (!confirm) return;
    setBusy(true);
    setDialogError(null);
    try {
      const { link, action } = confirm;
      if (action === "suspend") await suspendAdminFedLink(link.id);
      else if (action === "resume") await resumeAdminFedLink(link.id);
      else await deleteAdminFedLink(link.id);
      setConfirm(null);
      await load();
    } catch (e) {
      setDialogError(errText(e, "Request failed"));
    } finally {
      setBusy(false);
    }
  }

  async function applyState(run: () => Promise<FedState>) {
    setSwitching(true);
    setError(null);
    try {
      setState(await run());
      await load();
    } catch (e) {
      setError(errText(e, "Could not change federation"));
    } finally {
      setSwitching(false);
      setOffConfirm(false);
    }
  }

  const enabledFamilies = state ? state.families.filter((f) => f.enabled).length : 0;
  const verb = (a: Action | undefined) => (a === "delete" ? "Remove" : a === "suspend" ? "Suspend" : "Resume");

  return (
    <Stack spacing={3}>
      <Typography variant="h5">Federation</Typography>
      {error && <Alert severity="error">{error}</Alert>}

      {state && (
        <Card variant="outlined">
          <CardContent>
            <Stack spacing={1}>
              <FormControlLabel
                control={
                  <Switch
                    checked={state.enabled}
                    disabled={switching}
                    onChange={(e) => {
                      if (e.target.checked) void applyState(() => setFedEnabled(true));
                      else setOffConfirm(true);
                    }}
                  />
                }
                label="Federation"
              />
              <Typography variant="body2" color="text.secondary">
                Lets a family on this server link to a family on another server and exchange messages. Family admins
                make the links. Off by default.
              </Typography>
              {state.enabled && state.enabledAt && (
                <Typography variant="caption" color="text.secondary">
                  On since {new Date(state.enabledAt).toLocaleString()}. Changes can take up to 30 seconds to reach
                  other servers.
                </Typography>
              )}
              <Typography variant="subtitle1" sx={{ pt: 1 }}>
                Families
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {enabledFamilies} of {state.families.length} families
                {state.enabled ? "" : ". Family switches take effect once federation is on."}
              </Typography>
              {state.families.map((f) => (
                <FormControlLabel
                  key={f.id}
                  control={
                    <Switch
                      size="small"
                      checked={f.enabled}
                      disabled={switching}
                      onChange={(e) => void applyState(() => setFedFamily(f.id, e.target.checked))}
                    />
                  }
                  label={f.name}
                />
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}

      <Dialog fullScreen={fullScreen} open={offConfirm} onClose={() => setOffConfirm(false)}>
        <DialogTitle>Turn federation off?</DialogTitle>
        <DialogContent>
          <DialogContentText>Messages to and from other families stop. Links and contacts are kept.</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOffConfirm(false)} disabled={switching}>
            Cancel
          </Button>
          <Button variant="contained" color="error" disabled={switching} onClick={() => void applyState(() => setFedEnabled(false))}>
            Turn off
          </Button>
        </DialogActions>
      </Dialog>

      {state?.enabled && (
        <>
          <Typography variant="subtitle1">Links</Typography>
          <TableContainer sx={responsiveTableSx([3, 5])}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Family</TableCell>
                  <TableCell>Linked to</TableCell>
                  <TableCell>Host</TableCell>
                  <TableCell>State</TableCell>
                  <TableCell>Queued</TableCell>
                  <TableCell align="right" />
                </TableRow>
              </TableHead>
              <TableBody>
                {links.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6}>
                      <Typography variant="body2" color="text.secondary">
                        No links yet.
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
                {links.map((l) => {
                  const st = linkStateLabel(l);
                  return (
                    <TableRow key={`${l.familyId}:${l.id}`}>
                      <TableCell>{l.familyName}</TableCell>
                      <TableCell>{l.remote.name}</TableCell>
                      <TableCell>{l.remote.host}</TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          color={st === "active" ? "success" : st === "offline" ? "error" : st === "reauth" ? "warning" : "default"}
                          label={st === "reauth" ? "needs re-authorizing" : st}
                        />
                      </TableCell>
                      <TableCell>{l.pending}</TableCell>
                      <TableCell align="right">
                        {l.state !== "reauth" && (
                          <Button
                            size="small"
                            onClick={() => {
                              setDialogError(null);
                              setConfirm({ link: l, action: l.state === "suspended" ? "resume" : "suspend" });
                            }}
                          >
                            {l.state === "suspended" ? "Resume" : "Suspend"}
                          </Button>
                        )}
                        <Button
                          size="small"
                          color="error"
                          onClick={() => {
                            setDialogError(null);
                            setConfirm({ link: l, action: "delete" });
                          }}
                        >
                          Remove
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}

      <Dialog fullScreen={fullScreen} open={confirm !== null} onClose={() => (busy ? undefined : setConfirm(null))}>
        <DialogTitle>
          {verb(confirm?.action)} the link between {confirm?.link.familyName} and {confirm?.link.remote.name}?
        </DialogTitle>
        <DialogContent>
          {dialogError && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {dialogError}
            </Alert>
          )}
          <DialogContentText>
            {confirm?.action === "delete" &&
              "Messages still waiting fail, and the other family's people stop being sendable. The other side is told if it can be reached."}
            {confirm?.action === "suspend" && "Nothing is sent to or accepted from that family until the link is resumed."}
            {confirm?.action === "resume" && "Sending to and receiving from that family starts again."}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="contained"
            color={confirm?.action === "delete" ? "error" : "primary"}
            disabled={busy}
            onClick={() => void runConfirm()}
          >
            {verb(confirm?.action)}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

export default function AdminFederationPage() {
  return <FederationInner />;
}
