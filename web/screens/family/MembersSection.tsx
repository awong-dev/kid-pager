"use client";

/** "Family members" on `/family/people`: the table of people in this family, an Add person dialog
 * and a row-click `MemberDrawer`. Members arrive as props (the page subscribes once). */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import MemberDrawer from "@/components/MemberDrawer";
import SectionBlock from "@/components/SectionBlock";
import { Link } from "@/lib/router";
import { deviceName } from "@/lib/devices";
import { ApiError, api } from "@/lib/api";
import { AUTH_BACKEND } from "@/lib/auth";
import { familyQuery, useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { familyDevicesQuery } from "@/lib/live/queries";
import { inboundLabel, outboundLabel } from "@/lib/policy";
import type { DeviceDoc } from "@/lib/types";
import { responsiveTableSx } from "@/lib/tableSx";
import { permissionsHref } from "./permissionsModel";
import { AddPersonDialog, InviteDialog, type InviteCode, SignInEmailDialog } from "./MemberDialogs";
import type { FamilyMembers, MemberRow } from "./useFamilyMembers";

interface DeviceRow extends DeviceDoc {
  id: string;
}

const detail = (err: unknown, fallback: string): string =>
  err instanceof ApiError ? String(err.detail ?? err.message) : fallback;

export default function MembersSection({ members, loading, error: loadError }: FamilyMembers) {
  const { familyId } = useFamily();
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [selected, setSelected] = useState<MemberRow | null>(null);
  const [signInFor, setSignInFor] = useState<MemberRow | null>(null);
  const [invite, setInvite] = useState<InviteCode | null>(null);

  // A devices failure only drops the chips; the members table does not depend on it.
  const { docs: deviceDocs } = useLive<DeviceDoc>(familyId ? familyDevicesQuery(familyId) : null);
  const devicesByOwner = useMemo(() => {
    const map = new Map<string, DeviceRow[]>();
    for (const d of deviceDocs) {
      const list = map.get(d.data.ownerUid) ?? [];
      list.push({ id: d.id, ...d.data });
      map.set(d.data.ownerUid, list);
    }
    return map;
  }, [deviceDocs]);

  async function toggleDisabled(m: MemberRow) {
    setError(null);
    try {
      await api.patch(`/family/members/${m.uid}${familyQuery()}`, { disabled: !m.disabled });
    } catch (err) {
      setError(detail(err, "Failed to update person"));
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
      setError(detail(err, "Failed to create invite code"));
    }
  }

  return (
    <SectionBlock
      id="members"
      title="Family members"
      description="The people in your family: who has a pager and whether they can sign in. Tap a person to edit their profile; their messaging rules are under Permissions."
      action={
        <Button variant="contained" onClick={() => setCreateOpen(true)}>
          Add person
        </Button>
      }
    >
      {error && <Alert severity="error">{error}</Alert>}
      {loadError && <Alert severity="error">Could not load your family members. Try reloading the page.</Alert>}
      {loading && !loadError && <Skeleton variant="rounded" height={96} />}

      {!loading && !loadError && (
        <TableContainer sx={responsiveTableSx([4, 5, 6])}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Alias</TableCell>
                <TableCell>Role</TableCell>
                <TableCell>Sign-in</TableCell>
                <TableCell>Pagers</TableCell>
                <TableCell>Rules</TableCell>
                <TableCell>Enabled</TableCell>
                {AUTH_BACKEND === "home" && <TableCell />}
              </TableRow>
            </TableHead>
            <TableBody>
              {members.length === 0 && (
                <TableRow>
                  <TableCell colSpan={8}>
                    <Typography variant="body2" color="text.secondary">
                      No one yet. Use Add person.
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
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
                      <Button size="small" sx={{ ml: 1 }} onClick={() => setSignInFor(m)}>
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
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    <Chip
                      component={Link}
                      href={permissionsHref(m.alias)}
                      clickable
                      size="small"
                      variant="outlined"
                      label={`Can message ${outboundLabel(m.policy.out)} · Messaged by ${inboundLabel(m.policy.in)}`}
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
      )}

      {createOpen && <AddPersonDialog onClose={() => setCreateOpen(false)} />}
      {signInFor && <SignInEmailDialog member={signInFor} onClose={() => setSignInFor(null)} />}
      {invite && <InviteDialog invite={invite} onClose={() => setInvite(null)} />}
      <MemberDrawer member={selected} onClose={() => setSelected(null)} />
    </SectionBlock>
  );
}
