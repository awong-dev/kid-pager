"use client";

/** The banner every signed-in person sees on a server frozen for a move (docs/MOVE_DESIGN.md §7.5):
 * where to go, and their claim link or the email to sign in with. Shows nothing when there is no move. */

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { bannerKind, getMyMove, hostOf, targetLoginUrl } from "@/lib/move";
import { usePoll } from "@/lib/usePoll";

export default function MoveBanner() {
  const { data } = usePoll(getMyMove, 60_000);
  if (!data) return null;
  const host = hostOf(data.targetBaseUrl);
  const kind = bannerKind(data);
  const claim = data.claim?.url;
  return (
    <Alert severity="warning" sx={{ borderRadius: 0 }} role="region" aria-label="This server has moved">
      <Stack spacing={0.75}>
        <Typography variant="body2">
          <strong>This family moved to {host}.</strong> This server is read-only now: you can read, but not send.
        </Typography>
        {kind === "claim" && claim && (
          <>
            <Typography variant="body2">Open your link on the new server to choose a password or a passkey.</Typography>
            <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
              <Button size="small" variant="contained" component="a" href={claim} rel="noopener noreferrer">
                Sign in on {host}
              </Button>
              <Button size="small" onClick={() => void navigator.clipboard?.writeText(claim)}>
                Copy my link
              </Button>
            </Stack>
          </>
        )}
        {kind === "email" && (
          <Typography variant="body2">
            Sign in on the new server with <strong>{data.email}</strong>. Choose &quot;email me a sign-in link&quot;.{" "}
            <Link href={targetLoginUrl(data.targetBaseUrl)} rel="noopener noreferrer">
              Open the sign-in page
            </Link>
          </Typography>
        )}
        {kind === "plain" && (
          <Typography variant="body2">
            <Link href={targetLoginUrl(data.targetBaseUrl)} rel="noopener noreferrer">
              Go to the new server
            </Link>
          </Typography>
        )}
      </Stack>
    </Alert>
  );
}
