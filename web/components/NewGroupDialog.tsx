"use client";

/** Family-admin "create group" dialog; members are picked from `useDirectory().contacts`. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { familyQuery } from "@/lib/family-context";

// Same shape as the relay's `ALIAS_RE`; client-side validation only, the relay is the authority.
const ALIAS_RE = /^[a-z0-9][a-z0-9_-]{0,15}$/;

interface NewGroupDialogProps {
  open: boolean;
  onClose: () => void;
}

export default function NewGroupDialog({ open, onClose }: NewGroupDialogProps) {
  const { me } = useAuth();
  const { contacts } = useDirectory();
  const [name, setName] = useState("");
  const [alias, setAlias] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setName("");
    setAlias("");
    setSelected(new Set());
    setError(null);
  }

  function handleClose() {
    reset();
    onClose();
  }

  function toggle(uid: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) {
        next.delete(uid);
      } else {
        next.add(uid);
      }
      return next;
    });
  }

  const aliasValid = ALIAS_RE.test(alias);
  const formValid = name.trim().length > 0 && aliasValid && selected.size > 0;

  async function createGroup() {
    if (!me || !formValid) return;
    setBusy(true);
    setError(null);
    try {
      // The creating admin is included explicitly so they are a member of their own group.
      const memberUids = Array.from(new Set([me.uid, ...selected]));
      await api.post(`/family/groups${familyQuery()}`, { name: name.trim(), alias, memberUids });
      handleClose();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to create group");
    } finally {
      setBusy(false);
    }
  }

  const otherContacts = contacts.filter((c) => c.uid !== me?.uid);

  return (
      <DialogTitle>New group</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField label="Group name" value={name} onChange={(e) => setName(e.target.value)} fullWidth />
          <TextField
            label="Alias"
            value={alias}
            onChange={(e) => setAlias(e.target.value.toLowerCase())}
            helperText="lowercase, e.g. family -- this is the group's @alias, shared with user aliases"
            error={alias.length > 0 && !aliasValid}
            fullWidth
          />
          <Typography variant="subtitle2">Members</Typography>
          {otherContacts.length === 0 ? (
            <Alert severity="info">No other contacts yet.</Alert>
          ) : (
            <Stack spacing={0}>
              {otherContacts.map((c) => (
                <FormControlLabel
                  key={c.uid}
                  control={<Checkbox checked={selected.has(c.uid)} onChange={() => toggle(c.uid)} />}
                  label={`${c.displayName} (@${c.alias})`}
                  sx={{ ml: 0 }}
                />
              ))}
            </Stack>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Cancel</Button>
        <Button onClick={() => void createGroup()} disabled={!formValid || busy}>
          Create
        </Button>
      </DialogActions>
    </Dialog>
  );
}
