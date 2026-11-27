"use client";

/** The home sign-in form: username and password, passkey, or an admin-minted invite code to set a
 * password or add a passkey. */

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

import Logo from "@/components/Logo";
import { useNavigate } from "@/lib/router";
import { useAuth } from "@/lib/auth-context";
import {
  loginWithPasskey,
  loginWithPassword,
  passkeysAvailable,
  changePassword,
  redeemInvite,
  registerPasskey,
} from "@/lib/auth/home";
import { LOGIN_PASSWORD_MAX, parseInviteFragment, passwordChangeProblem } from "@/lib/move";
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
  const navigate = useNavigate();

  const [mode, setMode] = useState<Mode>("sign-in");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [canPasskey, setCanPasskey] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [changeTo, setChangeTo] = useState("");
  const [changeAgain, setChangeAgain] = useState("");

  useEffect(() => {
    // after mount: no `window` during prerender
    const t = setTimeout(() => {
      setCanPasskey(passkeysAvailable());
      // A claim link (`/login#invite=<code>`) opens the invite form with the code filled in; the
      // fragment is removed so the code does not stay in the address bar or history.
      const claim = parseInviteFragment(window.location.hash);
      if (claim) {
        setCode(claim);
        setMode("invite");
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
    }, 0);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (needsSetup) {
      navigate.replace("/setup");
    } else if (status === "signed-in" && mode !== "offer-passkey") {
      navigate.replace(takeReturnPath());
    }
  }, [status, needsSetup, mode, navigate]);

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
      if (!canPasskey) navigate.replace(takeReturnPath());
    }, "That invite code did not work.");

  const addPasskey = () =>
    run(async () => {
      await registerPasskey();
      navigate.replace(takeReturnPath());
    }, "Adding a passkey failed.");

  const changeProblem = passwordChangeProblem(currentPassword, changeTo, changeAgain);
  const change = () => run(() => changePassword(currentPassword, changeTo), "The password was not changed.");

  const signInValid = username.trim() !== "" && password !== "";
  const redeemValid = code.trim() !== "" && (newPassword === "" || (newPassword.length >= 8 && newPassword.length <= LOGIN_PASSWORD_MAX));

  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", p: 2 }}>
      <Paper elevation={2} sx={{ p: { xs: 2, sm: 4 }, maxWidth: 420, width: "100%" }}>
        <Box sx={{ display: "flex", justifyContent: "center", mb: 1 }}>
          <Logo size={72} />
        </Box>
        <Typography variant="h5" gutterBottom>
          Sign in to Pager
        </Typography>

        {notRegisteredMessage && <Alert severity="warning" sx={{ mb: 2 }}>{notRegisteredMessage}</Alert>}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {status === "must-change-password" && (
          <Stack spacing={2}>
            <Alert severity="info">
              Your admin set this password for you. Choose your own now; nothing else works until you do.
            </Alert>
            <TextField
              label="Password your admin gave you"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              fullWidth
            />
            <TextField
              label="New password"
              type="password"
              value={changeTo}
              onChange={(e) => setChangeTo(e.target.value)}
              helperText="At least 8 characters"
              autoComplete="new-password"
              fullWidth
            />
            <TextField
              label="New password again"
              type="password"
              value={changeAgain}
              onChange={(e) => setChangeAgain(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !changeProblem && void change()}
              autoComplete="new-password"
              fullWidth
            />
            {(changeTo !== "" || changeAgain !== "") && changeProblem && (
              <Typography variant="body2" color="error">
                {changeProblem}
              </Typography>
            )}
            <Button variant="contained" disabled={busy || !!changeProblem} onClick={() => void change()}>
              {busy ? "Saving..." : "Set my password"}
            </Button>
          </Stack>
        )}

        {status !== "must-change-password" && mode === "sign-in" && (
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

        {status !== "must-change-password" && mode === "invite" && (
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

        {status !== "must-change-password" && mode === "offer-passkey" && (
          <Stack spacing={2}>
            <Alert severity="success">You are signed in.</Alert>
            <Typography variant="body2">
              Add a passkey to sign in next time with this device&apos;s fingerprint, face or PIN.
            </Typography>
            <Button variant="contained" disabled={busy} onClick={() => void addPasskey()}>
              Add a passkey
            </Button>
            <Button disabled={busy} onClick={() => navigate.replace(takeReturnPath())}>
              Not now
            </Button>
          </Stack>
        )}
      </Paper>
    </Box>
  );
}
