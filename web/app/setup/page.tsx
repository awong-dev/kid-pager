"use client";

/** `/setup`: first-run page of a home install. While the relay has no users it creates the first
 * super admin (`POST /api/setup/first-admin`) and offers a passkey; otherwise it redirects away. */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { AUTH_BACKEND } from "@/lib/auth";
import {
  createFirstAdmin,
  getSetupStatus,
  passkeysAvailable,
  registerPasskey,
} from "@/lib/auth/home";

export default function SetupPage() {
  return AUTH_BACKEND === "home" ? <HomeSetup /> : <RedirectHome />;
}

function RedirectHome() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/");
  }, [router]);
  return null;
}

type Step = "checking" | "create" | "passkey";

function HomeSetup() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("checking");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const { needsSetup } = await getSetupStatus();
        if (needsSetup) {
          setStep("create");
        } else {
          router.replace("/login");
        }
      } catch (err) {
        setStep("create");
        setError(err instanceof Error ? err.message : "Could not reach the relay.");
      }
    })();
  }, [router]);

  async function create() {
    setError(null);
    setBusy(true);
    try {
      await createFirstAdmin(username.trim(), password);
      if (passkeysAvailable()) {
        setStep("passkey");
      } else {
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed.");
    } finally {
      setBusy(false);
    }
  }

  async function addPasskey() {
    setError(null);
    setBusy(true);
    try {
      await registerPasskey();
    } catch (err) {
      setError(
        err instanceof DOMException && err.name === "NotAllowedError"
          ? "The passkey prompt was cancelled or timed out."
          : err instanceof Error
            ? err.message
            : "Adding a passkey failed."
      );
    } finally {
      setBusy(false);
    }
  }

  const valid = username.trim() !== "" && password.length >= 8 && password === confirm;

  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", p: 2 }}>
      <Paper elevation={2} sx={{ p: { xs: 2, sm: 4 }, maxWidth: 420, width: "100%" }}>
        <Typography variant="h5" gutterBottom>
          Set up Pager
        </Typography>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {step === "checking" && (
          <Stack sx={{ alignItems: "center", py: 4 }}>
            <CircularProgress />
          </Stack>
        )}

        {step === "create" && (
          <Stack spacing={2}>
            <Typography variant="body2">
              This server has no accounts yet. Create the first administrator: it can add
              families, people and pagers.
            </Typography>
            <TextField
              label="Username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              helperText="Lowercase letters and digits; this is the sign-in name"
              autoComplete="username"
              autoCapitalize="none"
              autoFocus
              fullWidth
            />
            <TextField
              label="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              helperText="At least 8 characters"
              autoComplete="new-password"
              fullWidth
            />
            <TextField
              label="Confirm password"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              error={confirm !== "" && confirm !== password}
              onKeyDown={(e) => e.key === "Enter" && valid && void create()}
              autoComplete="new-password"
              fullWidth
            />
            <Button variant="contained" disabled={busy || !valid} onClick={() => void create()}>
              {busy ? "Creating..." : "Create administrator"}
            </Button>
          </Stack>
        )}

        {step === "passkey" && (
          <Stack spacing={2}>
            <Alert severity="success">Administrator created; you are signed in.</Alert>
            <Typography variant="body2">
              Add a passkey to sign in with this device&apos;s fingerprint, face or PIN. Your
              password keeps working either way.
            </Typography>
            <Button variant="contained" disabled={busy} onClick={() => void addPasskey()}>
              Add a passkey
            </Button>
              Not now
            </Button>
          </Stack>
        )}
      </Paper>
    </Box>
  );
}
