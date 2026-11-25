"use client";

/** The three dialogs of the Outside contacts section: Add contact, Rename and Delete
 * (`/api/family/contacts`). A duplicate name is a 409 shown in the dialog; any other failure is
 * handed to `onError` for the section's alert. Each is mounted only while open. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError, api } from "@/lib/api";
import { familyQuery } from "@/lib/family-context";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

const NAME_HELP = "Must be unique in the family; the pager shows the first 16 characters.";

const detail = (err: unknown, fallback: string): string =>
  err instanceof ApiError ? String(err.detail ?? err.message) : fallback;

/** Run `fn`; a 409 goes to `inDialog` (the dialog stays open), anything else to `elsewhere`. */
async function guarded(
  fn: () => Promise<void>,
  fallback: string,
  inDialog: (m: string) => void,
  elsewhere: (m: string) => void,
): Promise<boolean> {
  try {
    await fn();
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) inDialog(detail(err, fallback));
    else elsewhere(detail(err, fallback));
    return false;
  }
}

export function AddContactDialog({
  onClose,
  onDone,
  onError,
}: {
  onClose: () => void;
  onDone: () => void;
  onError: (m: string) => void;
}) {
  const fullScreen = useFullScreenDialog();
  const [form, setForm] = useState({ phone: "", name: "" });
  const [dialogError, setDialogError] = useState<string | null>(null);

  async function add() {
    setDialogError(null);
    const ok = await guarded(
      () => api.post(`/family/contacts${familyQuery()}`, { phone: form.phone.trim(), name: form.name.trim() }),
      "Failed to add contact",
      setDialogError,
      (m) => {
        onClose();
        onError(m);
      },
    );
    if (ok) {
      onClose();
      onDone();
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Add contact</DialogTitle>
      <DialogContent>
        {dialogError && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            {dialogError}
          </Alert>
        )}
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="Phone number"
            value={form.phone}
            onChange={(e) => setForm({ ...form, phone: e.target.value })}
            placeholder="+15551234567"
            fullWidth
          />
          <TextField
            label="Name"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            helperText={NAME_HELP}
            fullWidth
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={() => void add()} disabled={!form.phone.trim() || !form.name.trim()}>
          Add
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function RenameContactDialog({
  contact,
  onClose,
  onDone,
  onError,
}: {
  contact: { uid: string; displayName: string };
  onClose: () => void;
  onDone: () => void;
  onError: (m: string) => void;
}) {
  const fullScreen = useFullScreenDialog();
  const [name, setName] = useState(contact.displayName);
  const [dialogError, setDialogError] = useState<string | null>(null);

  async function save() {
    setDialogError(null);
    const ok = await guarded(
      () => api.patch(`/family/contacts/${contact.uid}${familyQuery()}`, { name: name.trim() }),
      "Failed to rename contact",
      setDialogError,
      (m) => {
        onClose();
        onError(m);
      },
    );
    if (ok) {
      onClose();
      onDone();
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Rename contact</DialogTitle>
      <DialogContent>
        {dialogError && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            {dialogError}
          </Alert>
        )}
        <TextField
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          helperText={NAME_HELP}
          fullWidth
          sx={{ mt: 1 }}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={() => void save()} disabled={!name.trim()}>
          Save
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function DeleteContactDialog({
  contact,
  onClose,
  onDone,
  onError,
}: {
  contact: { uid: string; displayName: string };
  onClose: () => void;
  onDone: () => void;
  onError: (m: string) => void;
}) {
  const fullScreen = useFullScreenDialog();

  async function remove() {
    try {
      await api.del(`/family/contacts/${contact.uid}${familyQuery()}`);
      onClose();
      onDone();
    } catch (err) {
      onClose();
      onError(detail(err, "Failed to delete contact"));
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Delete contact</DialogTitle>
      <DialogContent>
        <Typography>Delete {contact.displayName}? It is removed from every pager&apos;s SMS list.</Typography>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button color="error" onClick={() => void remove()}>
          Delete
        </Button>
      </DialogActions>
    </Dialog>
  );
}
