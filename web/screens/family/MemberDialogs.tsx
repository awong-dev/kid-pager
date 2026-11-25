"use client";

/** The three dialogs of the Family members section: Add person (`POST /api/family/members`), Add
 * sign-in email (`POST /api/family/people/{uid}/account`) and the one-time Invite code display. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { Link } from "@/lib/router";
import { ApiError, api, createPersonAccount } from "@/lib/api";
import { AUTH_BACKEND } from "@/lib/auth";
import { looksLikeEmail } from "@/lib/emailList";
import { familyQuery } from "@/lib/family-context";
import type { Role } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

const detail = (err: unknown, fallback: string): string =>
  err instanceof ApiError ? String(err.detail ?? err.message) : fallback;

const emptyForm = { alias: "", displayName: "", email: "", phone: "", role: "member" as Role };

/** Mounted only while open, so the form starts empty each time. */
export function AddPersonDialog({ onClose }: { onClose: () => void }) {
  const fullScreen = useFullScreenDialog();
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState<string | null>(null);
  const valid =
    form.alias.trim() &&
    form.displayName.trim() &&
    (AUTH_BACKEND === "home" || form.email.trim() || form.phone.trim());

  async function create() {
    setError(null);
    try {
      await api.post(`/family/members${familyQuery()}`, {
        alias: form.alias,
        displayName: form.displayName,
        email: form.email || null,
        phone: form.phone || null,
        role: form.role,
      });
      onClose();
    } catch (err) {
      setError(detail(err, "Failed to add person"));
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Add person</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField
            label="Alias"
            value={form.alias}
            onChange={(e) => setForm({ ...form, alias: e.target.value.toLowerCase() })}
            fullWidth
          />
          <TextField
            label="Display name"
            value={form.displayName}
            onChange={(e) => setForm({ ...form, displayName: e.target.value })}
            fullWidth
          />
          <TextField
            label="Email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            fullWidth
          />
          <TextField
            label="Sign-in phone (+1XXXXXXXXXX)"
            value={form.phone}
            onChange={(e) => setForm({ ...form, phone: e.target.value })}
            helperText={
              <>
                Used to sign in. Texts from this number to a family bridge phone count as this person. To text a number
                from a pager, add it under <Link href="/family/people#contacts">Outside contacts</Link>.
              </>
            }
            fullWidth
          />
          <TextField
            select
            label="Role"
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
            fullWidth
          >
            <MenuItem value="member">Member</MenuItem>
            <MenuItem value="admin">Admin</MenuItem>
          </TextField>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={() => void create()} disabled={!valid}>
          Create
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function SignInEmailDialog({
  member,
  onClose,
}: {
  member: { uid: string; displayName: string };
  onClose: () => void;
}) {
  const fullScreen = useFullScreenDialog();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function add() {
    setError(null);
    try {
      await createPersonAccount(member.uid, email.trim(), familyQuery());
      onClose();
    } catch (err) {
      setError(detail(err, "Failed to add sign-in email"));
    }
  }

  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Add sign-in email for {member.displayName}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField autoFocus label="Email" value={email} onChange={(e) => setEmail(e.target.value)} fullWidth />
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={() => void add()} disabled={!looksLikeEmail(email)}>
          Add
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export interface InviteCode {
  name: string;
  code: string;
  expiresAt: string;
}

export function InviteDialog({ invite, onClose }: { invite: InviteCode; onClose: () => void }) {
  const fullScreen = useFullScreenDialog();
  return (
    <Dialog fullScreen={fullScreen} open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Invite code</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Typography variant="h4" sx={{ fontFamily: "monospace", textAlign: "center" }}>
            {invite.code}
          </Typography>
          <Typography variant="body2">Valid until {new Date(invite.expiresAt).toLocaleString()}</Typography>
          <Typography variant="body2">
            Give this code to {invite.name}. On the sign-in page they choose &quot;Have an invite code?&quot; and set
            their own password or passkey. It works once.
          </Typography>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
