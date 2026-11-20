"use client";

/** Top-ups of one bridge phone (`GET /api/family/bridges/{id}/topups`; admin or bridge owner).
 * The relay says whether the button may be pressed (`allowed`, `reason`); `compact` shows that. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import { getTopups, requestTopup } from "@/lib/bridges";
import type { TopupRow, TopupsOut } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

const POLL_MS = 10_000;

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? String(e.detail ?? e.message) : fallback;
}

function policyText(t: TopupsOut): string {
  if (t.mode === "off") return "Top-ups are off for this family.";
  if (t.mode === "unlimited") return `Top-ups are unlimited. ${t.used} used this month.`;
  return `${t.used} of ${t.perMonth ?? "?"} top-ups used this month.`;
}

const STATE_COLOR: Record<string, "default" | "success" | "error" | "warning"> = {
  requested: "default",
  confirmed: "success",
  failed: "error",
  unconfirmed: "warning",
};

function HistoryRow({ row }: { row: TopupRow }) {
  return (
    <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
      <Typography variant="caption" color="text.secondary">
        {new Date(row.at).toLocaleString()}
      </Typography>
      <Typography variant="body2">{row.packLabel}</Typography>
      <Chip size="small" variant="outlined" color={STATE_COLOR[row.state] ?? "default"} label={row.state} />
      {row.note && (
        <Typography variant="caption" color="text.secondary">
          {row.note}
        </Typography>
      )}
    </Stack>
  );
}

export default function BridgeTopups({
  bridgeId,
  carrierName,
  compact = false,
}: {
  bridgeId: string;
  carrierName?: string;
  compact?: boolean;
}) {
  const fullScreen = useFullScreenDialog();
  const [data, setData] = useState<TopupsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ id: string; label: string; texts: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getTopups(bridgeId));
      setError(null);
    } catch (e) {
      setError(errText(e, "Could not load top-ups"));
    }
  }, [bridgeId]);

  useEffect(() => {
    const first = setTimeout(() => void load(), 0);
    const poll = compact ? undefined : setInterval(() => void load(), POLL_MS);
    return () => {
      clearTimeout(first);
      if (poll) clearInterval(poll);
    };
  }, [load, compact]);

  async function press() {
    if (!confirm) return;
    setBusy(true);
    setDialogError(null);
    try {
      await requestTopup(bridgeId, confirm.id);
      setConfirm(null);
      await load();
    } catch (e) {
      setDialogError(errText(e, "Top-up request failed"));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!data) return null;
  const pack = data.packs[0];

  return (
    <Stack spacing={0.75}>
      {!compact && <Typography variant="subtitle2">Top-ups</Typography>}
      {!compact && (
        <Typography variant="body2" color="text.secondary">
          {policyText(data)}
        </Typography>
      )}
      <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
        {pack && (
          <Button
            size="small"
            variant="outlined"
            disabled={!data.allowed}
            onClick={() => {
              setDialogError(null);
              setConfirm({ id: pack.id, label: pack.label, texts: pack.texts });
            }}
          >
            Top up {pack.label}
          </Button>
        )}
        {!data.allowed && data.reason && (
          <Typography variant="caption" color="text.secondary">
            {data.reason}
          </Typography>
        )}
      </Stack>
      {!compact && data.history.length > 0 && (
        <Stack spacing={0.5}>
          {data.history.slice(0, 10).map((h) => (
            <HistoryRow key={h.id} row={h} />
          ))}
        </Stack>
      )}

      <Dialog fullScreen={fullScreen} open={confirm !== null} onClose={() => (busy ? undefined : setConfirm(null))} maxWidth="xs">
        <DialogTitle>Top up {confirm?.label}?</DialogTitle>
        <DialogContent>
          {dialogError && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {dialogError}
            </Alert>
          )}
          <DialogContentText>
            This buys {confirm?.texts} texts on the {carrierName ?? "carrier"} account for this phone. The
            carrier charges the account&apos;s payment method.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)} disabled={busy}>
            Cancel
          </Button>
          <Button variant="contained" disabled={busy} onClick={() => void press()}>
            Buy texts
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
