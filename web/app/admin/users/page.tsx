"use client";

/** `/admin/users`: create (alias, name, email/phone, role), disable, delete via the users API. */

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DeleteIcon from "@mui/icons-material/Delete";
import IconButton from "@mui/material/IconButton";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import type { Role, UserDoc } from "@/lib/types";

interface UserRow extends UserDoc {
  uid: string;
}


function AdminUsersInner() {
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);


  async function createUser() {
    setError(null);
    try {
      await api.post("/admin/users", {
        alias: form.alias,
        displayName: form.displayName,
        email: form.email || null,
        phone: form.phone || null,
        role: form.role,
      });
      setCreateOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to create user");
    }
  }

  async function toggleDisabled(u: UserRow) {
    setError(null);
    try {
      await api.patch(`/admin/users/${u.uid}`, { disabled: !u.disabled });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to update user");
    }
  }

  async function deleteUser(u: UserRow) {
    setError(null);
    if (!window.confirm(`Delete @${u.alias}? This does not delete their message history.`)) return;
    try {
      await api.del(`/admin/users/${u.uid}`);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to delete user");
    }
  }


  return (
    <Stack spacing={2}>
        <Typography variant="h5">Users</Typography>
          Create user
        </Button>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}

            </TableRow>
        <DialogTitle>Create user</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Alias"
              value={form.alias}
              onChange={(e) => setForm({ ...form, alias: e.target.value })}
              helperText="lowercase, e.g. mom"
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
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
              fullWidth
            />
            <TextField
              select
              label="Role"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
              fullWidth
            >
              <MenuItem value="member">member</MenuItem>
              <MenuItem value="admin">admin</MenuItem>
            </TextField>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button onClick={() => void createUser()} disabled={!formValid}>
            Create
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

export default function AdminUsersPage() {
  return (
      <AppShell>
        <AdminUsersInner />
      </AppShell>
    </RequireAuth>
  );
}
