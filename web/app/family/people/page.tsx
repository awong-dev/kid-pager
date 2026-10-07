"use client";

/** `/family/people`: the family's members, an "Add person" dialog (`POST /api/family/members`)
 * and a row-click `MemberDrawer`. */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
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
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import AppShell from "@/components/AppShell";
import MemberDrawer, { type MemberDrawerMember } from "@/components/MemberDrawer";
import RequireAuth from "@/components/RequireAuth";
import { familyQuery, useFamily } from "@/lib/family-context";
import { inboundLabel, outboundLabel } from "@/lib/policy";
import type { DeviceDoc, FamilyDoc, Role, UserDoc } from "@/lib/types";

interface MemberRow extends UserDoc {
  uid: string;
}

interface DeviceRow extends DeviceDoc {
  id: string;
}

const emptyForm = { alias: "", displayName: "", email: "", phone: "", role: "member" as Role };

function FamilyPeopleInner() {
  const { familyId } = useFamily();
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [selected, setSelected] = useState<MemberDrawerMember | null>(null);
  const [familyName, setFamilyName] = useState<string | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);

  const loadFamily = useCallback(async () => {
    try {
      const fam = await api.get<FamilyDoc>(`/family${familyQuery()}`);
      setFamilyName(fam.name);
    } catch {
      // Header just stays blank; the members table is unaffected.
    }
  }, []);

  useEffect(() => {
    if (!familyId) return;
    let live = true;
    api
      .get<FamilyDoc>(`/family${familyQuery()}`)
      .then((fam) => {
      })
      .catch(() => {
        // Header stays blank; the members table is unaffected.
      });
    return () => {
      live = false;
    };
  }, [familyId]);

  async function saveRename() {
    setRenameError(null);
    try {
      await api.patch(`/family${familyQuery()}`, { name: renameValue.trim() });
      setRenameOpen(false);
      await loadFamily();
    } catch (err) {
      setRenameError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to rename family");
    }
  }

  const renameTrimmed = renameValue.trim();
  const renameValid = renameTrimmed.length >= 1 && [...renameTrimmed].length <= 40;


  const devicesByOwner = useMemo(() => {
    const map = new Map<string, DeviceRow[]>();
    for (const d of devices) {
      const list = map.get(d.ownerUid) ?? [];
      list.push(d);
      map.set(d.ownerUid, list);
    }
    return map;
  }, [devices]);

  async function createMember() {
    setError(null);
    try {
      await api.post(`/family/members${familyQuery()}`, {
        alias: form.alias,
        displayName: form.displayName,
        email: form.email || null,
        phone: form.phone || null,
        role: form.role,
      });
      setCreateOpen(false);
      setForm(emptyForm);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to add person");
    }
  }

  async function toggleDisabled(m: MemberRow) {
    setError(null);
    try {
      await api.patch(`/family/members/${m.uid}${familyQuery()}`, { disabled: !m.disabled });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to update person");
    }
  }


  return (
    <Stack spacing={2}>
        <Typography variant="h5">People</Typography>
        <Button variant="contained" onClick={() => setCreateOpen(true)}>
          Add person
        </Button>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}

        <Typography variant="body1">Family: {familyName ?? "--"}</Typography>
        <Button
          size="small"
          onClick={() => {
            setRenameValue(familyName ?? "");
            setRenameError(null);
            setRenameOpen(true);
          }}
        >
          Rename
        </Button>
      </Stack>

        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Name</TableCell>
              <TableCell>Alias</TableCell>
              <TableCell>Role</TableCell>
              <TableCell>Sign-in</TableCell>
              <TableCell>Devices</TableCell>
              <TableCell>Policy</TableCell>
              <TableCell>Enabled</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {members.map((m) => (
              <TableRow key={m.uid} hover sx={{ cursor: "pointer" }} onClick={() => setSelected(m)}>
                <TableCell>{m.displayName}</TableCell>
                <TableCell>@{m.alias}</TableCell>
                <TableCell>
                  <Chip
                    size="small"
                    label={m.role}
                    color={m.role === "admin" || m.role === "super" ? "primary" : "default"}
                  />
                </TableCell>
                <TableCell>
                  <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap" }}>
                    {(devicesByOwner.get(m.uid) ?? []).map((d) => (
                      <Link
                        key={d.id}
                        href={`/devices/${d.id}`}
                        onClick={(e) => e.stopPropagation()}
                        style={{ textDecoration: "none" }}
                      >
                      </Link>
                    ))}
                  </Stack>
                </TableCell>
                <TableCell>
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`Out: ${outboundLabel(m.policy.out)} · In: ${inboundLabel(m.policy.in)}`}
                  />
                </TableCell>
                <TableCell onClick={(e) => e.stopPropagation()}>
                  <Switch checked={!m.disabled} onChange={() => void toggleDisabled(m)} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

        <DialogTitle>Add person</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
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
                  <Link href="/family/contacts">Contacts</Link>.
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
          <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button onClick={() => void createMember()} disabled={!formValid}>
            Create
          </Button>
        </DialogActions>
      </Dialog>

        <DialogTitle>Rename family</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              autoFocus
              label="Family name"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              helperText="1-40 characters"
              fullWidth
            />
            {renameError && <Alert severity="error">{renameError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRenameOpen(false)}>Cancel</Button>
          <Button onClick={() => void saveRename()} disabled={!renameValid}>
            Save
          </Button>
        </DialogActions>
      </Dialog>

      <MemberDrawer member={selected} onClose={() => setSelected(null)} />
    </Stack>
  );
}

export default function FamilyPeoplePage() {
  return (
    <RequireAuth requireRole="admin">
      <AppShell>
        <FamilyPeopleInner />
      </AppShell>
    </RequireAuth>
  );
}
