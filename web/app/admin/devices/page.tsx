"use client";

/** `/admin/devices`: add, rotate credentials, revoke, lock controls, last status. The one-time
 * `setupCode` lives only in React state; the auto-lock select shows the last requested value. */

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useDirectory } from "@/lib/directory";
import type { DeviceDoc, UserDoc } from "@/lib/types";

import SetupCodePanel, { type SetupCodeResult } from "./SetupCodePanel";

// `POST /api/admin/devices/{id}/ca` with `{"action":"push"|"unpin"}`.
type CaAction = "push" | "unpin";

interface CaConfirmState {
  deviceId: string;
  label: string;
  action: CaAction;
}


// Response of device create and rotate-credentials; only the fields read here.
interface DeviceSetupResponse {
  device: { id: string };
  setupCode: string;
  expiresAt: string;
  brokerPush: "pushed" | "manual";
  manualAcl?: string[] | null;
}

function DevicesInner() {
  const { uidToAlias } = useDirectory();
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [setupResult, setSetupResult] = useState<SetupCodeResult | null>(null);
  const [caConfirm, setCaConfirm] = useState<CaConfirmState | null>(null);
  const [caBusy, setCaBusy] = useState(false);
  const [caError, setCaError] = useState<string | null>(null);
  const [caSuccess, setCaSuccess] = useState<string | null>(null);

  async function createDevice() {
    setError(null);
    try {
      const resp = await api.post<DeviceSetupResponse>("/admin/devices", {
        ownerAlias: form.ownerAlias,
        label: form.label,
        defaultToAlias: form.defaultToAlias || null,
      });
      setCreateOpen(false);
      setSetupResult({
        deviceId: resp.device.id,
        label: form.label,
        setupCode: resp.setupCode,
        expiresAt: resp.expiresAt,
        brokerPush: resp.brokerPush,
        manualAcl: resp.manualAcl,
      });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to create device");
    }
  }

  async function rotate(device: DeviceRow) {
    setError(null);
    try {
      const resp = await api.post<DeviceSetupResponse>(
        `/admin/devices/${device.id}/rotate-credentials`
      );
      setSetupResult({
        deviceId: device.id,
        label: device.label,
        setupCode: resp.setupCode,
        expiresAt: resp.expiresAt,
        brokerPush: resp.brokerPush,
        manualAcl: resp.manualAcl,
      });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to rotate credentials");
    }
  }

  async function revoke(deviceId: string) {
    setError(null);
    if (!window.confirm(`Revoke device ${deviceId}? This deletes its broker credential immediately.`)) {
      return;
    }
    try {
      await api.post(`/admin/devices/${deviceId}/revoke`);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to revoke device");
    }
  }

  async function deleteDevice(deviceId: string) {
    setError(null);
    if (!window.confirm(`Delete device ${deviceId}? This cannot be undone.`)) return;
    try {
      await api.del(`/admin/devices/${deviceId}`);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to delete device");
    }
  }

  async function setAutoLock(deviceId: string, minutes: number) {
    setError(null);
    try {
      await api.post(`/admin/devices/${deviceId}/cfg`, { lock: { auto: minutes } });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to set auto-lock");
    }
  }

  async function clearPasscode(deviceId: string) {
    setError(null);
    if (!window.confirm(`Clear ${deviceId}'s passcode? The pager unlocks immediately.`)) return;
    try {
      await api.post(`/admin/devices/${deviceId}/cfg`, { lock: { clear: true } });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to clear passcode");
    }
  }

  // Asynchronous: the chip updates when the pager's next `/status` arrives.
  async function submitCaAction() {
    if (!caConfirm) return;
    setCaBusy(true);
    setCaError(null);
    try {
      await api.post(`/admin/devices/${caConfirm.deviceId}/ca`, { action: caConfirm.action });
      setCaConfirm(null);
      setCaSuccess("Sent to the pager; it applies on its next connection.");
    } catch (err) {
      setCaError(
        err instanceof ApiError
          ? String(err.detail ?? err.message)
          : "Failed to update CA trust for this device."
      );
    } finally {
      setCaBusy(false);
    }
  }

  const setupResultDevice = setupResult
    ? devices.find((d) => d.id === setupResult.deviceId)
    : undefined;
  const setupResultOnline = setupResultDevice?.provisionState === "provisioned";

  return (
    <Stack spacing={2}>
        <Typography variant="h5">Devices</Typography>
          Add device
        </Button>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}


        <DialogTitle>Add device</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Label"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              fullWidth
            />
            <TextField
              select
              label="Owner"
              value={form.ownerAlias}
              onChange={(e) => setForm({ ...form, ownerAlias: e.target.value })}
              fullWidth
            >
                <MenuItem key={u.uid} value={u.alias}>
                  @{u.alias}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              label="Default recipient (optional)"
              value={form.defaultToAlias}
              onChange={(e) => setForm({ ...form, defaultToAlias: e.target.value })}
              fullWidth
            >
              <MenuItem value="">(none -- broadcast to all allowed)</MenuItem>
              {users.map((u) => (
                <MenuItem key={u.uid} value={u.alias}>
                  @{u.alias}
                </MenuItem>
              ))}
            </TextField>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button onClick={() => void createDevice()} disabled={!formValid}>
            Create
          </Button>
        </DialogActions>
      </Dialog>

      {setupResult && (
        <SetupCodePanel
          result={setupResult}
          online={setupResultOnline}
          onClose={() => setSetupResult(null)}
        />
      )}

        <DialogTitle>
          {caConfirm?.action === "push" ? "Push CA certificate" : "Un-pin CA certificate"}
        </DialogTitle>
        <DialogContent>
          {caError && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {caError}
            </Alert>
          )}
          <DialogContentText>
            {caConfirm?.action === "push" ? (
              <>
                This sends <strong>{caConfirm.label}</strong> ({caConfirm.deviceId}) a pointer to
                this relay&apos;s current CA certificate. The pager fetches it, verifies its hash,
                and switches to validating the broker&apos;s certificate the next time it
                reconnects. This does not happen immediately.
              </>
            ) : (
              <>
                This tells <strong>{caConfirm?.label}</strong> ({caConfirm?.deviceId}) to stop
                validating the broker&apos;s certificate. The pager will connect without
                verifying who it is talking to. Use this only to recover a device stuck unable to
                connect; it does not happen immediately.
              </>
            )}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCaConfirm(null)} disabled={caBusy}>
            Cancel
          </Button>
          <Button
            onClick={() => void submitCaAction()}
            disabled={caBusy}
            color={caConfirm?.action === "unpin" ? "warning" : "primary"}
          >
            {caConfirm?.action === "push" ? "Push CA" : "Un-pin CA"}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={caSuccess !== null}
        autoHideDuration={5000}
        onClose={() => setCaSuccess(null)}
        message={caSuccess}
      />
    </Stack>
  );
}

export default function DevicesPage() {
  return (
      <AppShell>
        <DevicesInner />
      </AppShell>
    </RequireAuth>
  );
}
