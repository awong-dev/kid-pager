"use client";

/** Roster table for a bridged Google Chat group: each speaker gets a nick (the pager's `sndr`),
 * defaulted by `slugNick` and validated against `NICK_RE` and for uniqueness. */

import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { NICK_RE } from "@/lib/bridges";
import type { RosterEntry } from "@/lib/types";

/** One error (or null) per roster row. */
export function rosterErrors(roster: RosterEntry[]): (string | null)[] {
  const counts = new Map<string, number>();
  for (const r of roster) counts.set(r.nick, (counts.get(r.nick) ?? 0) + 1);
  return roster.map((r) => {
    if (!NICK_RE.test(r.nick)) return "Lowercase letters, digits, - or _; up to 16";
    if ((counts.get(r.nick) ?? 0) > 1) return "Must be unique";
    return null;
  });
}

export default function ChatRosterEditor({
  roster,
  onChange,
}: {
  roster: RosterEntry[];
  onChange: (next: RosterEntry[]) => void;
}) {
  const errors = rosterErrors(roster);
  return (
    <Stack spacing={1}>
            <TableRow>
            </TableRow>
      <Typography variant="caption" color="text.secondary">
        People who have not spoken yet appear here when they do.
      </Typography>
    </Stack>
  );
}
