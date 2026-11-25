"use client";

/** `/login`: email (link flow back here with `?finish=1`, or password) or phone (reCAPTCHA, code)
 * over Firebase Auth. A 403 from `GET /api/me` arrives as `notRegisteredMessage`. */

import {
  type ConfirmationResult,
  RecaptchaVerifier,
  isSignInWithEmailLink,
  sendSignInLinkToEmail,
  signInWithEmailAndPassword,
  signInWithEmailLink,
  signInWithPhoneNumber,
} from "firebase/auth";
import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { useNavigate } from "@/lib/router";
import { AUTH_BACKEND } from "@/lib/auth";
import { useAuth } from "@/lib/auth-context";
import { getFirebaseAuth } from "@/lib/firebase";

import HomeLogin from "./HomeLogin";
import { takeReturnPath } from "@/lib/returnPath";

const EMAIL_LINK_STORAGE_KEY = "pager.signInEmail";
const E164_RE = /^\+[1-9]\d{1,14}$/;

type Mode = "enter-identifier" | "email-sent" | "enter-code" | "completing";

/** The home build (`NEXT_PUBLIC_BACKEND=home`) renders `HomeLogin` instead of the Firebase form. */
export default function LoginPage() {
  return AUTH_BACKEND === "home" ? <HomeLogin /> : <FirebaseLogin />;
}

function FirebaseLogin() {
  const { status, notRegisteredMessage } = useAuth();
  const navigate = useNavigate();

  const [mode, setMode] = useState<Mode>("enter-identifier");
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const confirmationRef = useRef<ConfirmationResult | null>(null);
  const recaptchaContainerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (status === "signed-in") {
      navigate.replace(takeReturnPath());
    }
  }, [status, navigate]);

  // completing an email-link sign-in (`?finish=1`)
  useEffect(() => {
    // async IIFE so state is set in a callback, not synchronously in the effect
    void (async () => {
      if (typeof window === "undefined") return;
      const params = new URLSearchParams(window.location.search);
      if (params.get("finish") !== "1") return;

      const auth = getFirebaseAuth();
      if (!isSignInWithEmailLink(auth, window.location.href)) {
        setError("This sign-in link is invalid or has expired. Request a new one below.");
        return;
      }
      let email = window.localStorage.getItem(EMAIL_LINK_STORAGE_KEY);
      if (!email) {
        email = window.prompt("Confirm the email address you signed in with:");
      }
      if (!email) {
        setError("Sign-in needs the email address you started with.");
        return;
      }
      setMode("completing");
      try {
        await signInWithEmailLink(auth, email, window.location.href);
        window.localStorage.removeItem(EMAIL_LINK_STORAGE_KEY);
        // AuthProvider redirects once `GET /api/me` resolves
      } catch (err) {
        setMode("enter-identifier");
        setError(err instanceof Error ? err.message : "Sign-in failed.");
      }
    })();
  }, []);

  async function handleSubmitIdentifier() {
    setError(null);
    const value = identifier.trim();
    if (!value) return;
    setBusy(true);
    try {
      if (value.includes("@") && password) {
        const auth = getFirebaseAuth();
        await signInWithEmailAndPassword(auth, value, password);
      } else if (value.includes("@")) {
        const auth = getFirebaseAuth();
        const url = new URL(window.location.href);
        url.search = "?finish=1";
        await sendSignInLinkToEmail(auth, value, {
          url: url.toString(),
          handleCodeInApp: true,
          // no linkDomain: Google rejects it (auth/invalid-hosting-link-domain) on the default
          // web.app/firebaseapp.com domain
        });
        window.localStorage.setItem(EMAIL_LINK_STORAGE_KEY, value);
        setMode("email-sent");
      } else if (E164_RE.test(value)) {
        const auth = getFirebaseAuth();
        if (!recaptchaContainerRef.current) {
          throw new Error("reCAPTCHA container missing");
        }
        const verifier = new RecaptchaVerifier(auth, recaptchaContainerRef.current, {
          size: "invisible",
        });
        confirmationRef.current = await signInWithPhoneNumber(auth, value, verifier);
        setMode("enter-code");
      } else {
        setError("Enter a valid email address or a phone number in +1XXXXXXXXXX (E.164) format.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  async function handleSubmitCode() {
    if (!confirmationRef.current) return;
    setError(null);
    setBusy(true);
    try {
      await confirmationRef.current.confirm(code.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : "That code did not match.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", p: 2 }}>
      <Paper elevation={2} sx={{ p: { xs: 2, sm: 4 }, maxWidth: 420, width: "100%" }}>
        <Typography variant="h5" gutterBottom>
          Sign in to Pager
        </Typography>

        {notRegisteredMessage && <Alert severity="warning" sx={{ mb: 2 }}>{notRegisteredMessage}</Alert>}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {mode === "completing" && (
          <Stack sx={{ alignItems: "center", py: 4 }}>
            <CircularProgress />
            <Typography sx={{ mt: 2 }}>Completing sign-in...</Typography>
          </Stack>
        )}

        {mode === "enter-identifier" && (
          <Stack spacing={2}>
            <TextField
              label="Email or phone (+1XXXXXXXXXX)"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !password && void handleSubmitIdentifier()}
              autoFocus
              fullWidth
            />
            {identifier.includes("@") && (
              <TextField
                label="Password (leave blank for an emailed sign-in link instead)"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void handleSubmitIdentifier()}
                fullWidth
              />
            )}
            <Button variant="contained" disabled={busy} onClick={() => void handleSubmitIdentifier()}>
              {busy
                ? identifier.includes("@") && password
                  ? "Signing in..."
                  : "Sending..."
                : identifier.includes("@") && password
                  ? "Sign in"
                  : "Continue"}
            </Button>
          </Stack>
        )}

        {mode === "email-sent" && (
          <Alert severity="success">
            Check your email for a sign-in link. Opening it on this device finishes sign-in.
          </Alert>
        )}

        {mode === "enter-code" && (
          <Stack spacing={2}>
            <Typography variant="body2">Enter the 6-digit code sent by SMS.</Typography>
            <TextField
              label="Code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void handleSubmitCode()}
              autoFocus
              fullWidth
            />
            <Button variant="contained" disabled={busy} onClick={() => void handleSubmitCode()}>
              {busy ? "Verifying..." : "Verify"}
            </Button>
          </Stack>
        )}

        {/* invisible reCAPTCHA anchor for `signInWithPhoneNumber` */}
        <div ref={recaptchaContainerRef} />
      </Paper>
    </Box>
  );
}
