"use client";

/** `/account/notifications`: browser push via `registerForPush()` (Web Push or FCM), test button,
 * and a family-admin "Family alerts" switch (`PATCH /api/me {notify: {alerts}}`). */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Typography from "@mui/material/Typography";

import { useAuth } from "@/lib/auth-context";
import { ApiError, api } from "@/lib/api";
import {
  type PushPath,
  currentPushPath,
  notificationPermission,
  notificationsSupported,
  registerForPush,
  showLocalTestNotification,
} from "@/lib/notifications";

const PATH_LABELS: Record<PushPath, string> = {
  webpush: "standard Web Push (VAPID, from this server)",
  fcm: "Firebase Cloud Messaging",
};

function NotificationsInner() {
  const { me, isFamilyAdmin } = useAuth();
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(() =>
    notificationPermission()
  );
  const [hasToken, setHasToken] = useState(false);
  // `undefined` while loading; `null` path = no push available here.
  const [pushPath, setPushPath] = useState<PushPath | null | undefined>(undefined);
  const [tlsMode, setTlsMode] = useState<string | null>(null);
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

  useEffect(() => {
    let cancelled = false;
    currentPushPath()
      .then(({ path, subscribed }) => {
        if (cancelled) return;
        setPushPath(path);
        if (subscribed) setHasToken(true);
      })
      .catch(() => {
        if (!cancelled) setPushPath(null);
      });
    api
      .get<{ tlsMode?: string }>("/me")
      .then((r) => {
        if (!cancelled) setTlsMode(r.tlsMode ?? null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
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
      const path = await registerForPush();
      setPermission(notificationPermission());
      if (!path) {
        setError(
          notificationPermission() === "denied"
            ? "Notifications are blocked for this site in the browser's site settings."
            : "This browser does not support web push (see the iOS note below)."
        );
        return;
      }
      setPushPath(path);
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

      {pushPath !== undefined && (
        <Typography variant="body2" color="text.secondary" data-testid="push-path">
          Delivery:{" "}
          <strong>{pushPath ? PATH_LABELS[pushPath] : "not available on this server"}</strong>
          {pushPath && (hasToken ? " (this browser is registered)" : " (this browser is not registered yet)")}
        </Typography>
      )}

      {tlsMode === "selfsigned" && (
        <Alert severity="warning">
          This server uses its own certificate, not a publicly trusted one. Browsers only deliver
          push notifications to a site whose certificate they trust, so notifications need the
          server switched to Let&apos;s Encrypt (TLS_MODE=letsencrypt in pager.env).
        </Alert>
      )}

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

      <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: "wrap" }}>
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
        permission and display work, not the push delivery path end to end.
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
  return <NotificationsInner />;
}
