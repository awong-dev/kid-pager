"use client";

/** Read-only list of people linked across families on this server (`allow` edges whose
 * `familyIds` hold two distinct families), under Outside contacts. Linking is done by a superadmin. */

import { useMemo } from "react";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { allowEdgesOfFamilyQuery } from "@/lib/live/queries";
import type { AllowEdgeDoc } from "@/lib/types";
import { responsiveTableSx } from "@/lib/tableSx";

export default function LinkedPeople() {
  const { familyId } = useFamily();
  const { byUid } = useDirectory();
  const { docs, loading, error } = useLive<AllowEdgeDoc>(familyId ? allowEdgesOfFamilyQuery(familyId) : null);
  const edges = useMemo(
    () =>
      docs
        .filter((d) => new Set(d.data.familyIds ?? []).size === 2)
        .map((d) => ({ key: d.id, fromUid: d.data.fromUid, toUid: d.data.toUid })),
    [docs],
  );
  const label = (uid: string): string => {
    const entry = byUid(uid);
    return entry ? `@${entry.alias}` : uid;
  };

  return (
    <Stack spacing={1}>
      <Typography variant="subtitle1" component="h3">
        People linked from other families
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Ask your superadmin to link a person from another family.
      </Typography>
      {error ? (
        <Typography variant="body2" color="error">
          Could not load these links.
        </Typography>
      ) : loading ? (
        <Skeleton variant="rounded" height={40} />
      ) : edges.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No links to other families yet.
        </Typography>
      ) : (
        <TableContainer sx={responsiveTableSx([])}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>From</TableCell>
                <TableCell>To</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {edges.map((e) => (
                <TableRow key={e.key}>
                  <TableCell>{label(e.fromUid)}</TableCell>
                  <TableCell>{label(e.toUid)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Stack>
  );
}
