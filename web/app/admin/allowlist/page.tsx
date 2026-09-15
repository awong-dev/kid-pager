"use client";

/** `/admin/allowlist`: users x users message/locate matrix, saved by `PUT /api/admin/allowlist`. */

import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import type { AllowEdgeDoc, UserDoc } from "@/lib/types";

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
  const [matrix, setMatrix] = useState<Map<string, Cell>>(new Map());
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
      setMatrix((prev) => {
        // keep unsaved local edits
        if (dirty) return prev;
        const next = new Map<string, Cell>();
        return next;
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `dirty` is read inside the closure intentionally, not a resubscribe trigger
  }, []);

  const pairs = useMemo(() => {
    const out: { from: UserRow; to: UserRow }[] = [];
        if (from.uid === to.uid) continue;
        out.push({ from, to });
      }
    }
    return out;

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
      setDirty(false);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save allow-list");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack spacing={2}>
        <Typography variant="h5">Allow-list</Typography>
        <Button variant="contained" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? "Saving..." : "Save"}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary">
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}

            </TableRow>
    </Stack>
  );
}

export default function AllowlistPage() {
  return (
      <AppShell>
        <AllowlistInner />
      </AppShell>
    </RequireAuth>
  );
}
