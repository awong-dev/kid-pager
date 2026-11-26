"use client";

/** "Add people from <family>" dialog. A 409 `not_shared` means the member is not yet shared. */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import {
  addFedContact,
  getFedDirectory,
  type FedLink,
  type FedDirectoryItem,
  formatFedServer,
} from "@/lib/federation";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

export interface FedMember {
  uid: string;
  alias: string;
  displayName: string;
}

function problemText(e: unknown, linkName: string): string {
  if (!(e instanceof ApiError)) return "Request failed";
  const d = String(e.detail ?? e.message);
  if (e.status === 409 && d.startsWith("not_shared")) {
    return `That member is not shared with ${linkName} yet. Share them under People > Other families first.`;
  }
  if (e.status === 409 && d.startsWith("alias_taken")) return "That alias is taken. Choose another.";
  if (e.status === 409 && d.startsWith("name_taken")) return "A contact already has that name. Choose another.";
  return d;
}

export default function FedAddContactDialog({
  open,
  links,
  initialLinkId,
  members,
  onClose,
  onAdded,
}: {
  open: boolean;
  links: FedLink[];
  initialLinkId?: string;
  members: FedMember[];
  onClose: () => void;
  onAdded: () => void;
}) {
  const fullScreen = useFullScreenDialog();
  const active = links.filter((l) => l.state === "active");
  const [peerId, setPeerId] = useState(initialLinkId ?? "");
  const linkName = active.find((l) => l.id === peerId)?.remote.name ?? "that family";
  const [directory, setDirectory] = useState<FedDirectoryItem[] | null>(null);
  const [dirError, setDirError] = useState<string | null>(null);
  const [rid, setRid] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [alias, setAlias] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!peerId) return;
    let live = true;
    getFedDirectory(peerId)
      .then((d) => {
        if (live) {
          setDirectory(d);
          setDirError(null);
        }
      })
      .catch((e) => {
        if (live) {
          setDirectory([]);
          setDirError(problemText(e, "that family"));
        }
      });
    return () => {
      live = false;
    };
  }, [peerId]);

  function pickPeer(id: string) {
    setPeerId(id);
    setDirectory(null);
    setDirError(null);
    setRid("");
  }

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await addFedContact(peerId, {
        rid,
        members: chosen,
        ...(alias.trim() ? { alias: alias.trim() } : {}),
        ...(name.trim() ? { name: name.trim() } : {}),
      });
      onAdded();
      onClose();
    } catch (e) {
      setError(problemText(e, linkName));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open={open} onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Add people from another family</DialogTitle>
      <DialogContent>
        <Stack spacing={1.5} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField select label="Family" value={peerId} onChange={(e) => pickPeer(e.target.value)} fullWidth>
            {active.map((l) => (
              <MenuItem key={l.id} value={l.id}>
                {l.remote.name} ({formatFedServer(l.remote.host, l.remote.port, l.remote.path)})
              </MenuItem>
            ))}
          </TextField>
          {peerId && dirError && <Alert severity="warning">{dirError}</Alert>}
          {peerId && directory === null && (
            <Typography variant="caption" color="text.secondary">
              Loading the directory...
            </Typography>
          )}
          {directory && !dirError && (
            <TextField
              select
              label="Person"
              value={rid}
              onChange={(e) => setRid(e.target.value)}
              fullWidth
              helperText={directory.length === 0 ? "That family shares nobody with you yet." : undefined}
            >
              {directory.map((d) => (
                <MenuItem key={d.rid} value={d.rid}>
                  {d.name} (@{d.alias}){d.added ? " - already added" : ""}
                </MenuItem>
              ))}
            </TextField>
          )}
          {rid && (
            <>
              <Typography variant="body2">Contact for</Typography>
              <Stack>
                {members.map((m) => (
                  <FormControlLabel
                    key={m.uid}
                    control={
                      <Checkbox
                        checked={chosen.includes(m.uid)}
                        onChange={(e) =>
                          setChosen(e.target.checked ? [...chosen, m.uid] : chosen.filter((u) => u !== m.uid))
                        }
                      />
                    }
                    label={`${m.displayName} (@${m.alias})`}
                  />
                ))}
              </Stack>
              <TextField
                label="Alias (optional)"
                value={alias}
                onChange={(e) => setAlias(e.target.value.toLowerCase())}
                helperText="Up to 16 characters. Leave empty to use theirs."
                fullWidth
              />
              <TextField
                label="Name (optional)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                helperText="Must be unique in the family. Leave empty to use theirs."
                fullWidth
              />
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={busy || !peerId || !rid || chosen.length === 0} onClick={() => void submit()}>
          Add
        </Button>
      </DialogActions>
    </Dialog>
  );
}
