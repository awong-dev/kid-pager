"use client";

/** `/settings/notifications`: browser push via `registerForPush()` (Web Push or FCM), test button,
 * and a family-admin "Family alerts" switch (`PATCH /api/me {notify: {alerts}}`). */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Typography from "@mui/material/Typography";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { useAuth } from "@/lib/auth-context";
import { ApiError, api } from "@/lib/api";
import {
  notificationPermission,
  notificationsSupported,
  registerForPush,
  showLocalTestNotification,
} from "@/lib/notifications";

function NotificationsInner() {
  const { me, isFamilyAdmin } = useAuth();
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(() =>
    notificationPermission()
  );
  const [hasToken, setHasToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [alertsEnabled, setAlertsEnabled] = useState(() => me?.notify.alerts ?? true);
  const [alertsSaving, setAlertsSaving] = useState(false);
  const [alertsError, setAlertsError] = useState<string | null>(null);

  // Keep `permission` live: the user can change it in browser site settings
  // while this page is open.
  useEffect(() => {
    const refresh = () => setPermission(notificationPermission());
    refresh();
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    let status: PermissionStatus | null = null;
    let cancelled = false;
    try {
      navigator.permissions
        ?.query({ name: "notifications" })
        .then((s) => {
          if (cancelled) return;
          status = s;
          s.onchange = refresh;
        })
        .catch(() => {});
    } catch {
      // Browsers that throw on this permission name: events above suffice.
    }
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
      if (status) status.onchange = null;
    };
  }, []);

  async function handleTest() {
    setError(null);
    try {
      await showLocalTestNotification();
    } catch (err) {
      setError(
        `Could not show the test notification: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  }

  async function handleAlertsToggle(next: boolean) {
    const previous = alertsEnabled;
    setAlertsEnabled(next);
    setAlertsSaving(true);
    setAlertsError(null);
    try {
      await api.patch("/me", { notify: { alerts: next } });
    } catch (err) {
      setAlertsEnabled(previous);
      setAlertsError(
        err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save"
      );
    } finally {
      setAlertsSaving(false);
    }
  }

  async function handleEnable() {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      setPermission(notificationPermission());
        setError(
          notificationPermission() === "denied"
            ? "Notifications are blocked for this site in the browser's site settings."
            : "This browser does not support web push (see the iOS note below)."
        );
        return;
      }
      setHasToken(true);
      setStatus("Notifications enabled for this browser.");
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to enable notifications");
    } finally {
      setPermission(notificationPermission());
      setBusy(false);
    }
  }

  return (
    <Stack spacing={2} sx={{ maxWidth: 480 }}>
      <Typography variant="h5">Notifications</Typography>

      {!notificationsSupported() && (
        <Alert severity="warning">This browser does not support the Notifications API.</Alert>
      )}

      <Typography variant="body2" color="text.secondary">
        Current permission: <strong>{permission}</strong>
      </Typography>

      {isFamilyAdmin && (
        <>
          <FormControlLabel
            control={
              <Switch
                checked={alertsEnabled}
                disabled={alertsSaving}
                onChange={(e) => void handleAlertsToggle(e.target.checked)}
              />
            }
            label="Family alerts"
          />
          {alertsError && <Alert severity="error">{alertsError}</Alert>}
        </>
      )}

      {permission === "granted" && !hasToken && (
        <Alert severity="info">
          Permission is granted. Press Enable notifications to register this browser.
        </Alert>
      )}
      {status && <Alert severity="success">{status}</Alert>}
      {error && <Alert severity="error">{error}</Alert>}

        <Button variant="contained" disabled={busy || !notificationsSupported()} onClick={() => void handleEnable()}>
          Enable notifications
        </Button>
        <Button
          variant="outlined"
          disabled={permission !== "granted"}
          onClick={() => void handleTest()}
        >
          Send test notification
        </Button>
      </Stack>

      <Alert severity="info">
        The test button shows a notification locally in this browser -- it does not round-trip
        through the relay (there is no &quot;send me a push&quot; API endpoint), so it only checks that
      </Alert>

      <Alert severity="info">
        iOS Safari: Apple only delivers push notifications to a PWA that has been added to the
        Home Screen (Share &rarr; Add to Home Screen) and opened from there at least once. Push
        will not work in a normal Safari tab.
      </Alert>
    </Stack>
  );
}

export default function NotificationsPage() {
  return (
    <RequireAuth>
      <AppShell>
        <NotificationsInner />
      </AppShell>
    </RequireAuth>
  );
}
