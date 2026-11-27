"use client";

/** `/admin/devices`: add, rotate credentials, revoke, lock controls, last status. The one-time
 * `setupCode` lives only in React state; the auto-lock select shows the last requested value. */

import { useMemo, useState } from "react";
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

import DeviceCard, { type DeviceRow, type PendingCfgDoc, type ProvisionState } from "@/components/DeviceCard";
import FirmwareUpdateDialog from "@/components/FirmwareUpdateDialog";
import FontsUpdateDialog from "@/components/FontsUpdateDialog";
import SoracomSimsSection from "@/components/SoracomSimsSection";
import { ApiError, api } from "@/lib/api";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { allDevicesQuery, allUsersQuery } from "@/lib/live/queries";
import { useNewestBuild } from "@/lib/firmware";
import type { DeviceDoc, UserDoc } from "@/lib/types";

import SetupCodePanel, { type SetupCodeResult } from "./SetupCodePanel";
import { DEVICE_LABEL_MAX_CHARS } from "@/lib/devices";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

// `POST /api/admin/devices/{id}/ca` with `{"action":"push"|"unpin"}`.
type CaAction = "push" | "unpin";

interface CaConfirmState {
  deviceId: string;
  label: string;
  action: CaAction;
}

const ALL_FAMILIES = "__all__";

const emptyForm = { ownerAlias: "", label: "", defaultToAlias: "", familyId: "" };

const BROKER_ERROR =
  "The broker did not accept the new login, so a setup code would not work. Make sure the broker is running and try again (for a new pager, use Rotate credentials).";

// Response of device create and rotate-credentials; only the fields read here.
interface DeviceSetupResponse {
  device: { id: string };
  setupCode: string;
  expiresAt: string;
  brokerPush: "pushed" | "manual";
  brokerResult: "ok" | "manual" | "error";
  manualAcl?: string[] | null;
}

function DevicesInner() {
  const fullScreen = useFullScreenDialog();
  const { uidToAlias } = useDirectory();
  const { familyId: scopeFamilyId, families } = useFamily();
  const { docs: deviceDocs } = useLive<
    DeviceDoc & { provisionState?: ProvisionState; pendingCfg?: PendingCfgDoc }
  >(allDevicesQuery());
  const devices = useMemo<DeviceRow[]>(() => deviceDocs.map((d) => ({ id: d.id, ...d.data })), [deviceDocs]);
  const { docs: userDocs } = useLive<UserDoc>(allUsersQuery());
  const users = useMemo<{ uid: string; alias: string; familyId: string | null }[]>(
    () => userDocs.map((d) => ({ uid: d.id, alias: d.data.alias, familyId: d.data.familyId ?? null })),
    [userDocs]
  );
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [setupResult, setSetupResult] = useState<SetupCodeResult | null>(null);
  const [caConfirm, setCaConfirm] = useState<CaConfirmState | null>(null);
  const [fwDeviceId, setFwDeviceId] = useState<string | null>(null);
  const [fontsDeviceId, setFontsDeviceId] = useState<string | null>(null);
  const newestBuild = useNewestBuild(true, "admin");
  const [caBusy, setCaBusy] = useState(false);
  const [caError, setCaError] = useState<string | null>(null);
  const [caSuccess, setCaSuccess] = useState<string | null>(null);
  // "All" only when the switcher has no family selected.
  const [filterFamily, setFilterFamily] = useState<string>(scopeFamilyId ?? ALL_FAMILIES);

  function openCreate() {
    // default the dialog's family picker to the switcher's family unless already picked
    setForm((f) => ({ ...f, familyId: f.familyId || scopeFamilyId || "" }));
    setCreateOpen(true);
  }

  const familyNameById = new Map(families.map((f) => [f.id, f.name]));
  const visibleDevices =
    filterFamily === ALL_FAMILIES ? devices : devices.filter((d) => d.familyId === filterFamily);
  const ownerOptions = form.familyId ? users.filter((u) => u.familyId === form.familyId) : users;

  async function createDevice() {
    setError(null);
    try {
      const resp = await api.post<DeviceSetupResponse>("/admin/devices", {
        ownerAlias: form.ownerAlias,
        label: form.label,
        defaultToAlias: form.defaultToAlias || null,
      });
      setCreateOpen(false);
      setForm({ ...emptyForm, familyId: scopeFamilyId ?? "" });
      if (resp.brokerResult === "error") {
        setError(BROKER_ERROR);
        return;
      }
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
      if (resp.brokerResult === "error") {
        setError(BROKER_ERROR);
        return;
      }
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

  const formValid = form.ownerAlias.trim() && form.label.trim();
  const setupResultDevice = setupResult
    ? devices.find((d) => d.id === setupResult.deviceId)
    : undefined;
  const setupResultOnline = setupResultDevice?.provisionState === "provisioned";

  return (
    <Stack spacing={2}>
      <Stack direction="row" sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h5">Devices</Typography>
        <Button variant="contained" onClick={openCreate}>
          Add device
        </Button>
      </Stack>
      <TextField
        select
        size="small"
        label="Family"
        value={filterFamily}
        onChange={(e) => setFilterFamily(e.target.value)}
        sx={{ maxWidth: 240 }}
      >
        <MenuItem value={ALL_FAMILIES}>All families</MenuItem>
        {families.map((f) => (
          <MenuItem key={f.id} value={f.id}>
            {f.name}
          </MenuItem>
        ))}
      </TextField>
      {error && <Alert severity="error">{error}</Alert>}

      <Stack spacing={2}>
        {visibleDevices.map((d) => (
          <DeviceCard
            key={d.id}
            device={d}
            scope="admin"
            newestBuild={newestBuild}
            ownerLabel={`@${uidToAlias(d.ownerUid) ?? d.ownerUid.slice(0, 8)}`}
            defaultToLabel={d.defaultToUid ? `@${uidToAlias(d.defaultToUid) ?? d.defaultToUid.slice(0, 8)}` : "--"}
            familyLabel={d.familyId ? (familyNameById.get(d.familyId) ?? d.familyId) : "--"}
            onRotate={() => void rotate(d)}
            onRevoke={() => void revoke(d.id)}
            onDelete={() => void deleteDevice(d.id)}
            onFirmware={() => setFwDeviceId(d.id)}
            onFonts={() => setFontsDeviceId(d.id)}
            onCa={(action) => setCaConfirm({ deviceId: d.id, label: d.label, action })}
            onSetAutoLock={(m) => void setAutoLock(d.id, m)}
            onClearPasscode={() => void clearPasscode(d.id)}
          />
        ))}
      </Stack>

      <SoracomSimsSection />

      <Dialog fullScreen={fullScreen} open={createOpen} onClose={() => setCreateOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Add device</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Label"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              helperText="Shown on the web and on the pager's device screen"
              slotProps={{ htmlInput: { maxLength: DEVICE_LABEL_MAX_CHARS } }}
              autoFocus
              fullWidth
            />
            <TextField
              select
              label="Family"
              value={form.familyId}
              onChange={(e) => setForm({ ...form, familyId: e.target.value, ownerAlias: "" })}
              helperText="Narrows the owner picker below; the device's own family is set from its owner."
              fullWidth
            >
              {families.map((f) => (
                <MenuItem key={f.id} value={f.id}>
                  {f.name}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              label="Owner"
              value={form.ownerAlias}
              onChange={(e) => setForm({ ...form, ownerAlias: e.target.value })}
              fullWidth
            >
              {ownerOptions.map((u) => (
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

      <Dialog fullScreen={fullScreen} open={caConfirm !== null} onClose={() => (caBusy ? undefined : setCaConfirm(null))}>
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

      {fontsDeviceId !== null && (
        <FontsUpdateDialog
          device={devices.find((x) => x.id === fontsDeviceId) ?? null}
          scope="admin"
          open
          onClose={() => setFontsDeviceId(null)}
        />
      )}

      {fwDeviceId !== null && (
        <FirmwareUpdateDialog
          device={devices.find((x) => x.id === fwDeviceId) ?? null}
          scope="admin"
          open
          onClose={() => setFwDeviceId(null)}
        />
      )}

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
  return <DevicesInner />;
}
