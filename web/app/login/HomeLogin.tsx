"use client";

/** The home sign-in form: username and password, passkey, or an admin-minted invite code to set a
 * password or add a passkey. */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Divider from "@mui/material/Divider";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { useAuth } from "@/lib/auth-context";
import {
  loginWithPasskey,
  loginWithPassword,
  passkeysAvailable,
  redeemInvite,
  registerPasskey,
} from "@/lib/auth/home";
import { takeReturnPath } from "@/lib/returnPath";

type Mode = "sign-in" | "invite" | "offer-passkey";

function message(err: unknown, fallback: string): string {
  if (err instanceof DOMException && err.name === "NotAllowedError") {
    return "The passkey prompt was cancelled or timed out.";
  }
  return err instanceof Error ? err.message : fallback;
}

export default function HomeLogin() {
  const { status, notRegisteredMessage, needsSetup } = useAuth();
  const router = useRouter();

  const [mode, setMode] = useState<Mode>("sign-in");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [canPasskey, setCanPasskey] = useState(false);

  useEffect(() => {
    // after mount: no `window` during prerender
    const t = setTimeout(() => setCanPasskey(passkeysAvailable()), 0);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (needsSetup) {
      router.replace("/setup");
    } else if (status === "signed-in" && mode !== "offer-passkey") {
      router.replace(takeReturnPath());
    }
  }, [status, needsSetup, mode, router]);

  async function run(fn: () => Promise<unknown>, fallback: string) {
    setError(null);
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      setError(message(err, fallback));
    } finally {
      setBusy(false);
    }
  }

  const signIn = () =>
    run(() => loginWithPassword(username.trim(), password), "Sign-in failed.");

  const signInWithPasskey = () => run(() => loginWithPasskey(), "Passkey sign-in failed.");

  const redeem = () =>
    run(async () => {
      // the redirect waits while a passkey is offered
      setMode("offer-passkey");
      try {
        await redeemInvite(code.trim(), newPassword || undefined);
      } catch (err) {
        setMode("invite");
        throw err;
      }
      if (!canPasskey) router.replace(takeReturnPath());
    }, "That invite code did not work.");

  const addPasskey = () =>
    run(async () => {
      await registerPasskey();
      router.replace(takeReturnPath());
    }, "Adding a passkey failed.");

  const signInValid = username.trim() !== "" && password !== "";
  const redeemValid = code.trim() !== "" && (newPassword === "" || newPassword.length >= 8);

  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", p: 2 }}>
      <Paper elevation={2} sx={{ p: { xs: 2, sm: 4 }, maxWidth: 420, width: "100%" }}>
        <Typography variant="h5" gutterBottom>
          Sign in to Pager
        </Typography>

        {notRegisteredMessage && <Alert severity="warning" sx={{ mb: 2 }}>{notRegisteredMessage}</Alert>}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {mode === "sign-in" && (
          <Stack spacing={2}>
            <TextField
              label="Username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username webauthn"
              autoCapitalize="none"
              autoFocus
              fullWidth
            />
            <TextField
              label="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && signInValid && void signIn()}
              autoComplete="current-password"
              fullWidth
            />
            <Button variant="contained" disabled={busy || !signInValid} onClick={() => void signIn()}>
              {busy ? "Signing in..." : "Sign in"}
            </Button>
            {canPasskey && (
              <>
                <Divider>or</Divider>
                <Button variant="outlined" disabled={busy} onClick={() => void signInWithPasskey()}>
                  Sign in with a passkey
                </Button>
              </>
            )}
            <Link component="button" variant="body2" onClick={() => { setError(null); setMode("invite"); }}>
              Have an invite code?
            </Link>
          </Stack>
        )}

        {mode === "invite" && (
          <Stack spacing={2}>
            <Typography variant="body2">
              Enter the code your admin gave you. Choose a password now, or leave it blank
              {canPasskey ? " and add a passkey next" : " to sign in once"}.
            </Typography>
            <TextField
              label="Invite code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoCapitalize="characters"
              autoComplete="one-time-code"
              autoFocus
              fullWidth
            />
            <TextField
              label="New password (optional)"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              helperText="At least 8 characters"
              error={newPassword !== "" && newPassword.length < 8}
              autoComplete="new-password"
              fullWidth
            />
            <Button variant="contained" disabled={busy || !redeemValid} onClick={() => void redeem()}>
              {busy ? "Checking..." : "Continue"}
            </Button>
            <Link component="button" variant="body2" onClick={() => { setError(null); setMode("sign-in"); }}>
              Back to sign-in
            </Link>
          </Stack>
        )}

        {mode === "offer-passkey" && (
          <Stack spacing={2}>
            <Alert severity="success">You are signed in.</Alert>
            <Typography variant="body2">
              Add a passkey to sign in next time with this device&apos;s fingerprint, face or PIN.
            </Typography>
            <Button variant="contained" disabled={busy} onClick={() => void addPasskey()}>
              Add a passkey
            </Button>
            <Button disabled={busy} onClick={() => router.replace(takeReturnPath())}>
              Not now
            </Button>
          </Stack>
        )}
      </Paper>
    </Box>
  );
}
