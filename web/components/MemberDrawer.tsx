"use client";

/** Row-click drawer on `/family/people`: Profile, Policy and Approved sections. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Divider from "@mui/material/Divider";
import Drawer from "@mui/material/Drawer";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import ApprovedEditor from "@/components/ApprovedEditor";
import PolicyPicker from "@/components/PolicyPicker";
import { ApiError, api } from "@/lib/api";
import { familyQuery } from "@/lib/family-context";
import type { Role, UserDoc } from "@/lib/types";

export interface MemberDrawerMember extends UserDoc {
  uid: string;
}

// Keyed by `member.uid` by the parent so form state initializes fresh per member.
function MemberProfileForm({
  member,
  onClose,
}: {
  member: MemberDrawerMember;
  onClose: () => void;
}) {
  const [displayName, setDisplayName] = useState(member.displayName);
  const [role, setRole] = useState<Role>(member.role);
  const [disabled, setDisabled] = useState(member.disabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/family/members/${member.uid}${familyQuery()}`, {
        displayName,
        role,
        disabled,
      });
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Typography variant="h6">@{member.alias}</Typography>

      <Typography variant="subtitle2">Profile</Typography>
      {error && <Alert severity="error">{error}</Alert>}
      <TextField
        label="Display name"
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        fullWidth
      />
      <TextField
        select
        label="Role"
        value={role}
        onChange={(e) => setRole(e.target.value as Role)}
        // A family admin can only promote/demote member<->admin; super is set in `/admin/users`.
        disabled={member.role === "super"}
        fullWidth
      >
        <MenuItem value="member">Member</MenuItem>
        <MenuItem value="admin">Admin</MenuItem>
        {member.role === "super" && <MenuItem value="super">Super</MenuItem>}
      </TextField>
      <FormControlLabel
        control={<Switch checked={!disabled} onChange={(e) => setDisabled(!e.target.checked)} />}
        label={disabled ? "Disabled" : "Enabled"}
      />
      <Button variant="contained" onClick={() => void save()} disabled={busy}>
        Save
      </Button>

      <Divider />

      <Typography variant="subtitle2">Policy</Typography>

      <Divider />

      <Typography variant="subtitle2">Approved</Typography>
    </>
  );
}

export default function MemberDrawer({
  member,
  onClose,
}: {
  /** `null` closes the drawer. */
  member: MemberDrawerMember | null;
  onClose: () => void;
}) {
  return (
    <Drawer anchor="right" open={member !== null} onClose={onClose}>
        {member && <MemberProfileForm key={member.uid} member={member} onClose={onClose} />}
      </Stack>
    </Drawer>
  );
}
