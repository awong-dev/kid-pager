"use client";

/** Row-click drawer on `/family/people`: Profile, Policy, Group chats and Approved. Profile is this
 * file's `MemberProfileForm`; Policy and Approved are `MemberPolicyParts` (the Permissions page
 * lifts them out of here); the drawer only keeps the saved outbound policy that links the two. */

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

import { Link } from "@/lib/router";
import { MemberApprovedPart, MemberPolicyPart } from "@/components/MemberPolicyParts";
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
  // The saved outbound policy (not the unsaved radio), so the Approved hint
  // matches what the relay applies.
  const [savedOut, setSavedOut] = useState(member.policy.out);
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
      <div>
        <Typography variant="caption" color="text.secondary">
          SMS number
        </Typography>
        <Typography>{member.smsNumber ?? "none"}</Typography>
        <Typography variant="caption" color="text.secondary">
          Set by the member&apos;s bridge phone (Pagers).
        </Typography>
      </div>
      <FormControlLabel
        control={<Switch checked={!disabled} onChange={(e) => setDisabled(!e.target.checked)} />}
        label={disabled ? "Disabled" : "Enabled"}
      />
      <Button variant="contained" onClick={() => void save()} disabled={busy}>
        Save
      </Button>

      <MemberPolicyPart member={member} onSaved={(p) => setSavedOut(p.out)} />

      <Divider />

      <Typography variant="subtitle2">Group chats</Typography>
      <Button component={Link} href={`/family/chat?uid=${encodeURIComponent(member.uid)}`} variant="outlined">
        Group chats…
      </Button>

      <MemberApprovedPart member={member} policyOut={savedOut} />
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
      <Stack spacing={2} sx={{ width: { xs: "100vw", sm: 420 }, maxWidth: "100vw", overflowY: "auto", p: 3 }}>
        {member && <MemberProfileForm key={member.uid} member={member} onClose={onClose} />}
      </Stack>
    </Drawer>
  );
}
