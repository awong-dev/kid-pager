"use client";

/** The "Nickname for ..." dialog of an address book: set or clear the owner's own nickname for one
 * entry (`PUT/DELETE /api/book/{owner}/entries/{peer}`). The parent re-keys it per entry. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import TextField from "@mui/material/TextField";

import { ApiError } from "@/lib/api";
import { type BookEntry, NICK_MAX_CODEPOINTS, nickError, saveNick } from "@/lib/book";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

export default function BookNickDialog({
  owner,
  entry,
  onClose,
  onSaved,
}: {
  owner: string;
  entry: BookEntry | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const fullScreen = useFullScreenDialog();
  // Re-keyed by the parent per entry, so the initial value is just the nick.
  const [value, setValue] = useState(entry?.nick ?? "");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const invalid = nickError(value);

  async function submit(next: string) {
    if (!entry?.uid) return;
    setBusy(true);
    setFailure(null);
    try {
      await saveNick(owner, entry.uid, next);
      onSaved();
      onClose();
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : "Could not save the nickname");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open={entry !== null} onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Nickname for {entry?.displayName}</DialogTitle>
      <DialogContent>
        <TextField
          autoFocus
          fullWidth
          margin="dense"
          label="Nickname"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          error={invalid !== null}
          helperText={invalid ?? `${[...value].length}/${NICK_MAX_CODEPOINTS}`}
        />
        {failure && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {failure}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        {entry?.nick && (
          <Button color="warning" disabled={busy} onClick={() => submit("")} sx={{ mr: "auto" }}>
            Clear
          </Button>
        )}
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={busy || invalid !== null || !value.trim()} onClick={() => submit(value)}>
          Save
        </Button>
      </DialogActions>
    </Dialog>
  );
}
