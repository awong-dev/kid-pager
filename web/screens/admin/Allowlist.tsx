"use client";

/** `/admin/allowlist`: users x users message/locate matrix, saved by `PUT /api/admin/allowlist`. */

import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { ApiError, api } from "@/lib/api";
import { useFamily } from "@/lib/family-context";
import { subscribeLive, useLive } from "@/lib/live";
import { allAllowQuery, allUsersQuery } from "@/lib/live/queries";
import type { AllowEdgeDoc, UserDoc } from "@/lib/types";
import { responsiveTableSx } from "@/lib/tableSx";

interface UserRow extends UserDoc {
  uid: string;
}

interface Cell {
  message: boolean;
  locate: boolean;
}

function pairKey(from: string, to: string): string {
  return `${from}_${to}`;
}

function AllowlistInner() {
  const { familyId: scopeFamilyId } = useFamily();
  const [matrix, setMatrix] = useState<Map<string, Cell>>(new Map());
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [showAllFamilies, setShowAllFamilies] = useState(false);

  const { docs: userDocs } = useLive<UserDoc>(allUsersQuery());
  const users = useMemo(() => {
    const rows: UserRow[] = userDocs.map((d) => ({ uid: d.id, ...d.data }));
    rows.sort((a, b) => a.alias.localeCompare(b.alias));
    return rows;
  }, [userDocs]);

  useEffect(() => {
    return subscribeLive<AllowEdgeDoc>(allAllowQuery(), (docs) => {
      setMatrix((prev) => {
        // keep unsaved local edits
        if (dirty) return prev;
        const next = new Map<string, Cell>();
        for (const d of docs) {
          next.set(pairKey(d.data.fromUid, d.data.toUid), { message: d.data.message, locate: d.data.locate });
        }
        return next;
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `dirty` is read inside the closure intentionally, not a resubscribe trigger
  }, []);

  // Filtered to the switcher family unless "Show all families"; cross-family Locate stays disabled.
  const visibleUsers = useMemo(
    () => (showAllFamilies ? users : users.filter((u) => u.familyId === scopeFamilyId)),
    [users, showAllFamilies, scopeFamilyId]
  );

  const pairs = useMemo(() => {
    const out: { from: UserRow; to: UserRow }[] = [];
    for (const from of visibleUsers) {
      for (const to of visibleUsers) {
        if (from.uid === to.uid) continue;
        out.push({ from, to });
      }
    }
    return out;
  }, [visibleUsers]);

  function cellFor(from: string, to: string): Cell {
    return matrix.get(pairKey(from, to)) ?? { message: false, locate: false };
  }

  function setCell(from: string, to: string, patch: Partial<Cell>) {
    setDirty(true);
    setMatrix((prev) => {
      const next = new Map(prev);
      const key = pairKey(from, to);
      next.set(key, { ...cellFor(from, to), ...patch });
      return next;
    });
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const entries = pairs
        .map(({ from, to }) => ({ from, to, cell: cellFor(from.uid, to.uid) }))
        .filter(({ cell }) => cell.message || cell.locate)
        .map(({ from, to, cell }) => ({
          fromAlias: from.alias,
          toAlias: to.alias,
          message: cell.message,
          locate: cell.locate,
        }));
      // Replace-all is scoped to `?family=` when filtered, so other families' edges survive.
      const path =
        !showAllFamilies && scopeFamilyId
          ? `/admin/allowlist?family=${encodeURIComponent(scopeFamilyId)}`
          : "/admin/allowlist";
      await api.put(path, { entries });
      setDirty(false);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save allow-list");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack spacing={2}>
      <Stack direction="row" sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h5">Allow-list</Typography>
        <Button variant="contained" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? "Saving..." : "Save"}
        </Button>
      </Stack>
      <FormControlLabel
        control={
          <Switch
            checked={showAllFamilies}
            onChange={(e) => setShowAllFamilies(e.target.checked)}
          />
        }
        label="Show all families"
      />
      <Typography variant="body2" color="text.secondary">
        Rows are the viewer (&quot;from&quot;), columns are the other person (&quot;to&quot;).
        &quot;Message&quot; allows sending to that person. &quot;Locate&quot; allows seeing that
        person&apos;s pager location on /location -- their current position, their
        history for the last 7 days, and requesting a fresh fix -- until this box is
        unchecked. No one, including an admin, can grant themselves this; only an
        admin can grant it to someone else here. Locate stays within a family, so it is
        disabled between two people in different families.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}

      <TableContainer sx={responsiveTableSx([], true)}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>From \ To</TableCell>
              {visibleUsers.map((u) => (
                <TableCell key={u.uid} align="center">
                  @{u.alias}
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {visibleUsers.map((from) => (
              <TableRow key={from.uid}>
                <TableCell>@{from.alias}</TableCell>
                {visibleUsers.map((to) => {
                  if (from.uid === to.uid) {
                    return <TableCell key={to.uid} align="center">--</TableCell>;
                  }
                  const cell = cellFor(from.uid, to.uid);
                  const crossFamily = from.familyId !== to.familyId;
                  return (
                    <TableCell key={to.uid} align="center">
                      <Stack direction="row" spacing={0} sx={{ justifyContent: "center" }}>
                        <Checkbox
                          size="small"
                          checked={cell.message}
                          title={`Allow @${from.alias} to message @${to.alias}`}
                          onChange={(e) => setCell(from.uid, to.uid, { message: e.target.checked })}
                        />
                        <Tooltip
                          title={
                            crossFamily
                              ? "Locate stays within a family"
                              : `Allow @${from.alias} to see @${to.alias}'s pager location`
                          }
                        >
                          <span>
                            <Checkbox
                              size="small"
                              checked={cell.locate}
                              disabled={crossFamily}
                              onChange={(e) => setCell(from.uid, to.uid, { locate: e.target.checked })}
                            />
                          </span>
                        </Tooltip>
                      </Stack>
                    </TableCell>
                  );
                })}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Stack>
  );
}

export default function AllowlistPage() {
  return <AllowlistInner />;
}
