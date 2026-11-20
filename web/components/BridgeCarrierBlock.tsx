"use client";

/** The Carrier block of a bridge phone card: carrier, texts left, last check, Check now, Set
 * balance and the carrier log. Family admin only (`/api/family/bridges/{id}/carrier*`). */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import { checkCarrierNow, getCarrier, setCarrierBalance, setCarrierId } from "@/lib/bridges";
import { formatRelativeAge } from "@/lib/time";
import type { CarrierOut, CarrierView } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

const SOURCE_LABELS: Record<NonNullable<CarrierView["source"]>, string> = {
  carrier: "from carrier",
  manual: "entered",
  estimate: "estimate",
};

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? String(e.detail ?? e.message) : fallback;
}

export function textsLeftLabel(c: CarrierView): string {
  if (c.unlimited) return "unlimited";
  if (c.texts === null) return "unknown";
  return `${c.texts}${c.source ? ` (${SOURCE_LABELS[c.source]})` : ""}`;
}

export default function BridgeCarrierBlock({ bridgeId, carrier }: { bridgeId: string; carrier: CarrierView }) {
  const fullScreen = useFullScreenDialog();
  const [out, setOut] = useState<CarrierOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [balanceOpen, setBalanceOpen] = useState(false);
  const [texts, setTexts] = useState("");
  const [unlimited, setUnlimited] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setOut(await getCarrier(bridgeId));
    } catch (e) {
      setError(errText(e, "Could not load the carrier"));
    }
  }, [bridgeId]);

  // reload the choices and the log when the polled `carrier` view adds a log row
  useEffect(() => {
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [load, carrier.lastCheckAt, carrier.balanceAt, carrier.id, carrier.unknownReplies]);

  async function act(fn: () => Promise<CarrierOut | unknown>, after?: () => void) {
    setBusy(true);
    setError(null);
    setDialogError(null);
    try {
      const res = await fn();
      if (res && typeof res === "object" && "carriers" in res) setOut(res as CarrierOut);
      else await load();
      after?.();
    } catch (e) {
      const msg = errText(e, "Request failed");
      if (balanceOpen) setDialogError(msg);
      else setError(msg);
    } finally {
      setBusy(false);
    }
  }

  const c = out?.carrier ?? carrier;
  const autoName = out?.carriers.find((x) => x.id === c.autoId)?.name ?? c.autoId;
  const level = c.level === "out" ? "error" : c.level === "low" ? "warning" : undefined;
  const balanceValid = unlimited || (/^\d+$/.test(texts) && Number(texts) <= 1_000_000);

  return (
    <Stack spacing={0.75}>
      <Typography variant="subtitle2">Carrier</Typography>
      {error && <Alert severity="error">{error}</Alert>}
      <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
        <TextField
          select
          size="small"
          label="Carrier"
          value={c.manual ? c.id : ""}
          disabled={busy || !out}
          onChange={(e) => void act(() => setCarrierId(bridgeId, e.target.value === "" ? null : e.target.value))}
          sx={{ minWidth: 200 }}
        >
          <MenuItem value="">Auto ({autoName})</MenuItem>
          {out?.carriers.map((x) => (
            <MenuItem key={x.id} value={x.id}>
              {x.name}
            </MenuItem>
          ))}
        </TextField>
        <Typography variant="body2">{c.manual ? "chosen by hand" : `matched: ${c.name}`}</Typography>
      </Stack>
      <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
        <Typography variant="body2">Texts left: {textsLeftLabel(c)}</Typography>
        {level && <Chip size="small" color={level} label={c.level === "out" ? "out of texts" : "low"} />}
        <Typography variant="caption" color="text.secondary">
          {c.lastCheckAt ? `checked ${formatRelativeAge(c.lastCheckAt * 1000)}` : "never checked"}
        </Typography>
      </Stack>
      {c.estimateOnly && (
        <Typography variant="caption" color="text.secondary">
          The carrier has not answered the last three checks, so this number is an estimate. See the log for what it
          said.
        </Typography>
      )}
      <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap" }}>
        <Button
          size="small"
          variant="outlined"
          disabled={busy || !c.canCheck}
          onClick={() => void act(() => checkCarrierNow(bridgeId))}
        >
          Check now
        </Button>
        <Button
          size="small"
          variant="outlined"
          disabled={busy}
          onClick={() => {
            setTexts(c.texts !== null ? String(c.texts) : "");
            setUnlimited(c.unlimited);
            setDialogError(null);
            setBalanceOpen(true);
          }}
        >
          Set balance
        </Button>
        <Button size="small" onClick={() => setLogOpen(!logOpen)}>
          Carrier log {logOpen ? "▴" : "▾"}
        </Button>
      </Stack>
      <Collapse in={logOpen} unmountOnExit>
        <Stack spacing={0.75}>
          {out?.log.length === 0 && (
            <Typography variant="caption" color="text.secondary">
              Nothing yet.
            </Typography>
          )}
          {out?.log.map((r) => (
            <div key={r.id}>
              <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: "center", flexWrap: "wrap" }}>
                <Typography variant="caption" color="text.secondary">
                  {r.at ? new Date(r.at).toLocaleString() : ""}
                </Typography>
                <Chip size="small" variant="outlined" label={`${r.dir === "in" ? "from" : "to"} ${r.party}`} />
                <Chip size="small" variant="outlined" label={r.kind} />
              </Stack>
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {r.text}
              </Typography>
              {r.detail && (
                <Typography variant="caption" color="text.secondary">
                  {r.detail}
                </Typography>
              )}
            </div>
          ))}
        </Stack>
      </Collapse>

      <Dialog fullScreen={fullScreen} open={balanceOpen} onClose={() => setBalanceOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Set balance</DialogTitle>
        <DialogContent>
          {dialogError && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {dialogError}
            </Alert>
          )}
          <TextField
            fullWidth
            margin="dense"
            label="Texts left"
            value={unlimited ? "" : texts}
            disabled={unlimited}
            onChange={(e) => setTexts(e.target.value)}
            slotProps={{ htmlInput: { inputMode: "numeric" } }}
            helperText="The relay subtracts each text the phone sends from this number."
          />
          <FormControlLabel
            control={<Checkbox checked={unlimited} onChange={(e) => setUnlimited(e.target.checked)} />}
            label="Unlimited plan"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setBalanceOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={busy || !balanceValid}
            onClick={() =>
              void act(
                () => setCarrierBalance(bridgeId, unlimited ? { unlimited: true } : { texts: Number(texts) }),
                () => setBalanceOpen(false)
              )
            }
          >
            Save
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
