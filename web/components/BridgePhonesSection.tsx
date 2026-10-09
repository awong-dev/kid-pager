"use client";

/** "Bridge phones" on `/family/devices`: a card per bridge phone, polled every 10 s (server-only
 * docs). Actions: Add (pairing panel), Reassign, Edit numbers, Accept SIM, New code, Unpair. */

import { useCallback, useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import BridgePairPanel from "@/components/BridgePairPanel";
import { ApiError } from "@/lib/api";
import {
  acceptSim,
  createBridge,
  listBridges,
  newBridgeCode,
  patchBridge,
  toMs,
  unpairBridge,
} from "@/lib/bridges";
import { formatRelativeAge } from "@/lib/time";

export interface BridgeMember {
  uid: string;
  alias: string;
  displayName: string;
}

const POLL_MS = 10_000;

interface PairState {
  bridgeId: string;
  code: string;
  expiresAt: ApiTime;
}

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? String(e.detail ?? e.message) : fallback;
}

function StatusChip({ label, ok }: { label: string; ok: boolean | undefined }) {
  return <Chip size="small" label={label} color={ok ? "success" : "default"} variant={ok ? "filled" : "outlined"} />;
}

export default function BridgePhonesSection({
  familyId,
  members,
}: {
  familyId: string | null;
  members: BridgeMember[];
}) {
  const [bridges, setBridges] = useState<BridgeRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  // each member's relay-derived SMS number

  const [addOpen, setAddOpen] = useState(false);
  const [addOwner, setAddOwner] = useState("");
  const [addLabel, setAddLabel] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [pair, setPair] = useState<PairState | null>(null);
  const [reassign, setReassign] = useState<{ id: string; owner: string } | null>(null);
  const [numbers, setNumbers] = useState<{ id: string; sim: string; voice: string } | null>(null);
  const [unpair, setUnpair] = useState<BridgeRow | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setBridges(await listBridges());
      setError(null);
    } catch (e) {
      setError(errText(e, "Could not load bridge phones"));
    }
  }, []);

  useEffect(() => {
    if (!familyId) return;
    const first = setTimeout(() => void refresh(), 0);
    const poll = setInterval(() => void refresh(), POLL_MS);
    return () => {
      clearTimeout(first);
      clearInterval(poll);
    };
  }, [familyId, refresh]);

  const memberLabel = useMemo(() => {
    const m = new Map(members.map((x) => [x.uid, `${x.displayName} (@${x.alias})`]));
    return (uid: string) => m.get(uid) ?? uid.slice(0, 8);
  }, [members]);

  const pairBridge = pair ? bridges.find((b) => b.id === pair.bridgeId) : undefined;

  async function run(fn: () => Promise<void>, done: () => void) {
    setDialogBusy(true);
    setDialogError(null);
    try {
      await fn();
      done();
      await refresh();
    } catch (e) {
      setDialogError(errText(e, "Request failed"));
    } finally {
      setDialogBusy(false);
    }
  }

  async function add() {
    setAddBusy(true);
    setAddError(null);
    try {
      const res = await createBridge(addOwner, addLabel.trim());
      setAddOpen(false);
      setAddLabel("");
      setPair({ bridgeId: res.bridge.id, code: res.code, expiresAt: res.expiresAt });
      setBridges((prev) => [...prev.filter((b) => b.id !== res.bridge.id), res.bridge]);
    } catch (e) {
      setAddError(errText(e, "Could not add the bridge phone"));
    } finally {
      setAddBusy(false);
    }
  }

  async function reissue(b: BridgeRow) {
    try {
      const res = await newBridgeCode(b.id);
      setPair({ bridgeId: b.id, code: res.code, expiresAt: res.expiresAt });
    } catch (e) {
      setError(errText(e, "Could not issue a new code"));
    }
  }

  async function accept(b: BridgeRow) {
    try {
      await acceptSim(b.id);
      await refresh();
    } catch (e) {
      setError(errText(e, "Could not accept the SIM number"));
    }
  }

  const memberSelect = (value: string, onChange: (v: string) => void) => (
    <TextField select fullWidth margin="dense" label="Member" value={value} onChange={(e) => onChange(e.target.value)}>
      {members.map((m) => (
        <MenuItem key={m.uid} value={m.uid}>
          {m.displayName} (@{m.alias})
        </MenuItem>
      ))}
    </TextField>
  );

  return (
    <Stack spacing={2}>
        <Typography variant="h5">Bridge phones</Typography>
        <Button
          variant="contained"
          onClick={() => {
            setAddOwner(members[0]?.uid ?? "");
            setAddError(null);
            setAddOpen(true);
          }}
        >
          Add bridge phone
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary">
        A headless Android phone that carries a member&apos;s Google Chat, Google Voice and SIM texts.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}


        <DialogTitle>Add bridge phone</DialogTitle>
        <DialogContent>
          {addError && <Alert severity="error" sx={{ mb: 1 }}>{addError}</Alert>}
          {memberSelect(addOwner, setAddOwner)}
          <TextField
            fullWidth
            margin="dense"
            label="Label"
            value={addLabel}
            onChange={(e) => setAddLabel(e.target.value)}
            helperText="For example: Pixel in the hall closet"
          />
          <Typography variant="caption" color="text.secondary">
            A phone signed into the member&apos;s own Google account posts in Chat as them.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAddOpen(false)}>Cancel</Button>
          <Button variant="contained" disabled={addBusy || !addOwner || !addLabel.trim()} onClick={() => void add()}>
            Create
          </Button>
        </DialogActions>
      </Dialog>

      {pair && pairBridge && (
        <BridgePairPanel
          bridge={pairBridge}
          memberLabel={memberLabel(pairBridge.ownerUid)}
          code={pair.code}
          expiresAt={pair.expiresAt}
          onEditNumbers={() =>
            setNumbers({ id: pairBridge.id, sim: pairBridge.simNumber ?? "", voice: pairBridge.voiceNumber ?? "" })
          }
          onClose={() => setPair(null)}
        />
      )}

        <DialogTitle>Reassign bridge phone</DialogTitle>
        <DialogContent>
          {dialogError && <Alert severity="error" sx={{ mb: 1 }}>{dialogError}</Alert>}
          {reassign && memberSelect(reassign.owner, (v) => setReassign({ ...reassign, owner: v }))}
          <Typography variant="caption" color="text.secondary">
            The old member&apos;s SMS number is cleared if it came from this phone; the new member&apos;s is set.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReassign(null)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={dialogBusy || !reassign}
            onClick={() => reassign && void run(async () => void (await patchBridge(reassign.id, { ownerUid: reassign.owner })), () => setReassign(null))}
          >
            Reassign
          </Button>
        </DialogActions>
      </Dialog>

        <DialogTitle>Phone numbers</DialogTitle>
        <DialogContent>
          {dialogError && <Alert severity="error" sx={{ mb: 1 }}>{dialogError}</Alert>}
          {numbers && (
            <>
              <TextField
                fullWidth
                margin="dense"
                label="SIM number"
                placeholder="+1 206 555 0100"
                value={numbers.sim}
                onChange={(e) => setNumbers({ ...numbers, sim: e.target.value })}
                helperText="Leave empty for a Voice-only phone."
              />
              <TextField
                fullWidth
                margin="dense"
                label="Google Voice number"
                placeholder="+1 206 555 0101"
                value={numbers.voice}
                onChange={(e) => setNumbers({ ...numbers, voice: e.target.value })}
                helperText="The member's SMS number is the SIM number, or the Voice number when there is no SIM."
              />
            </>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setNumbers(null)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={dialogBusy || !numbers}
            onClick={() =>
              numbers &&
              void run(
                async () =>
                  void (await patchBridge(numbers.id, {
                  })),
                () => setNumbers(null)
              )
            }
          >
            Save
          </Button>
        </DialogActions>
      </Dialog>

        <DialogTitle>Unpair {unpair?.label}?</DialogTitle>
        <DialogContent>
          {dialogError && <Alert severity="error" sx={{ mb: 1 }}>{dialogError}</Alert>}
          <DialogContentText>
            The phone stops working immediately. Texts waiting to send fail, and the member&apos;s SMS number is
            cleared if it came from this phone. Contacts and conversations are kept.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setUnpair(null)}>Cancel</Button>
          <Button
            color="error"
            disabled={dialogBusy}
            onClick={() => unpair && void run(async () => void (await unpairBridge(unpair.id)), () => setUnpair(null))}
          >
            Unpair
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
