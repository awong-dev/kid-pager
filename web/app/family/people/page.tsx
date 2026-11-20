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
import FedLinksSection from "@/components/FedLinksSection";
import FamilySettingsSection from "@/components/FamilySettingsSection";
import MemberDrawer, { type MemberDrawerMember } from "@/components/MemberDrawer";
import RequireAuth from "@/components/RequireAuth";
import { deviceName } from "@/lib/devices";
import { ApiError, api, createPersonAccount } from "@/lib/api";
import { AUTH_BACKEND } from "@/lib/auth";
import { looksLikeEmail } from "@/lib/emailList";
import { familyQuery, useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { familyDevicesQuery, familyMembersQuery } from "@/lib/live/queries";
import { inboundLabel, outboundLabel } from "@/lib/policy";
import type { DeviceDoc, FamilyDoc, Role, UserDoc } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";
import { responsiveTableSx } from "@/lib/tableSx";

interface MemberRow extends UserDoc {
  uid: string;
}

interface DeviceRow extends DeviceDoc {
  id: string;
}

const emptyForm = { alias: "", displayName: "", email: "", phone: "", role: "member" as Role };

function FamilyPeopleInner() {
  const fullScreen = useFullScreenDialog();
  const { familyId } = useFamily();
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [selected, setSelected] = useState<MemberDrawerMember | null>(null);
  const [familyName, setFamilyName] = useState<string | null>(null);
  const [familySettings, setFamilySettings] = useState<FamilyDoc["settings"]>(undefined);
  const [familyHooks, setFamilyHooks] = useState<NonNullable<FamilyDoc["hooks"]>>([]);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [familyFederation, setFamilyFederation] = useState<boolean | undefined>(undefined);
  const [signInFor, setSignInFor] = useState<MemberRow | null>(null);
  const [signInEmail, setSignInEmail] = useState("");
  const [signInError, setSignInError] = useState<string | null>(null);
  const [invite, setInvite] = useState<{ name: string; code: string; expiresAt: string } | null>(null);

  const loadFamily = useCallback(async () => {
    try {
      const fam = await api.get<FamilyDoc>(`/family${familyQuery()}`);
      setFamilyName(fam.name);
      setFamilyHooks(fam.hooks ?? []);
      setFamilySettings(fam.settings);
      setFamilyFederation(fam.federation);
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
        if (!live) return;
        setFamilyName(fam.name);
        setFamilyHooks(fam.hooks ?? []);
        setFamilySettings(fam.settings);
        setFamilyFederation(fam.federation);
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

  // No family in scope yet: `useLive` keeps what it held.
  const { docs: memberDocs } = useLive<UserDoc>(familyId ? familyMembersQuery(familyId) : null);
  const members = useMemo(() => {
    const rows: MemberRow[] = memberDocs.map((d) => ({ uid: d.id, ...d.data }));
    rows.sort((a, b) => a.alias.localeCompare(b.alias));
    return rows;
  }, [memberDocs]);
  const { docs: deviceDocs } = useLive<DeviceDoc>(familyId ? familyDevicesQuery(familyId) : null);
  const devices = useMemo<DeviceRow[]>(() => deviceDocs.map((d) => ({ id: d.id, ...d.data })), [deviceDocs]);

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

  async function createInvite(m: MemberRow) {
    setError(null);
    try {
      const res = await api.post<{ code: string; expiresAt: string }>(
        `/family/people/${m.uid}/invite${familyQuery()}`
      );
      setInvite({ name: m.displayName, ...res });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to create invite code");
    }
  }

  async function addSignIn() {
    if (!signInFor) return;
    setSignInError(null);
    try {
      await createPersonAccount(signInFor.uid, signInEmail.trim(), familyQuery());
      setSignInFor(null);
      setSignInEmail("");
    } catch (err) {
      setSignInError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to add sign-in email");
    }
  }

  const formValid =
    form.alias.trim() &&
    form.displayName.trim() &&
    (AUTH_BACKEND === "home" || form.email.trim() || form.phone.trim());

  return (
    <Stack spacing={2}>
      <Stack direction="row" sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h5">People</Typography>
        <Button variant="contained" onClick={() => setCreateOpen(true)}>
          Add person
        </Button>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}

      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
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

      <FamilySettingsSection
        settings={familySettings}
        onSaved={(fam) => {
          setFamilySettings(fam.settings);
        }}
      />

      {familyHooks.map((h) => (
        <Typography key={`${h.name}@${h.host}`} variant="body2" color="text.secondary">
          Messages in this family are also sent to: {h.name} ({h.host})
        </Typography>
      ))}

      <TableContainer sx={responsiveTableSx([4,5,6])}>
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
              {AUTH_BACKEND === "home" && <TableCell />}
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
                <TableCell onClick={(e) => e.stopPropagation()}>
                  {m.email ?? (m.phone ? `${m.phone} (sign-in)` : "--")}
                  {AUTH_BACKEND !== "home" && !m.email && !m.phone && m.kind === "person" && (
                    <Button
                      size="small"
                      sx={{ ml: 1 }}
                      onClick={() => {
                        setSignInFor(m);
                        setSignInEmail("");
                        setSignInError(null);
                      }}
                    >
                      Add sign-in email
                    </Button>
                  )}
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
                        <Chip size="small" label={deviceName(d)} clickable />
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
                {AUTH_BACKEND === "home" && (
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    {m.kind !== "external" && (
                      <Button size="small" onClick={() => void createInvite(m)}>
                        Invite code
                      </Button>
                    )}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <FedLinksSection
        federation={familyFederation}
        members={members.filter((m) => m.kind === "person" && !m.disabled)}
      />

      <Dialog fullScreen={fullScreen} open={createOpen} onClose={() => setCreateOpen(false)} fullWidth maxWidth="xs">
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
                  Used to sign in. Texts from this number to a family bridge phone count as this person. To text a number from a pager, add it under{" "}
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

      <Dialog fullScreen={fullScreen} open={signInFor !== null} onClose={() => setSignInFor(null)} fullWidth maxWidth="xs">
        <DialogTitle>Add sign-in email for {signInFor?.displayName}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              autoFocus
              label="Email"
              value={signInEmail}
              onChange={(e) => setSignInEmail(e.target.value)}
              fullWidth
            />
            {signInError && <Alert severity="error">{signInError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSignInFor(null)}>Cancel</Button>
          <Button onClick={() => void addSignIn()} disabled={!looksLikeEmail(signInEmail)}>
            Add
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog fullScreen={fullScreen} open={renameOpen} onClose={() => setRenameOpen(false)} fullWidth maxWidth="xs">
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

      <Dialog fullScreen={fullScreen} open={invite !== null} onClose={() => setInvite(null)} fullWidth maxWidth="xs">
        <DialogTitle>Invite code</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <Typography variant="h4" sx={{ fontFamily: "monospace", textAlign: "center" }}>
              {invite?.code}
            </Typography>
            <Typography variant="body2">
              Valid until {invite ? new Date(invite.expiresAt).toLocaleString() : ""}
            </Typography>
            <Typography variant="body2">
              Give this code to {invite?.name}. On the sign-in page they choose &quot;Have an invite code?&quot; and set
              their own password or passkey. It works once.
            </Typography>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setInvite(null)}>Close</Button>
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
