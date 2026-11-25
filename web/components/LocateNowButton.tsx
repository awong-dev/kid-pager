"use client";

/** "Locate now": asks the owner's pager for a fresh position (`POST /api/conversations/{alias}/locate`)
 * and tells the viewer what happens next in a snackbar. Used by `/location` and the Messages
 * dashboard cards; the reply arrives as a new fix, not as a response to this call. */

import { useState } from "react";
import Button from "@mui/material/Button";
import Snackbar from "@mui/material/Snackbar";
import LocationOnIcon from "@mui/icons-material/LocationOn";

import { ApiError, api } from "@/lib/api";

export default function LocateNowButton({ alias, label = "Locate now" }: { alias: string | undefined; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [snack, setSnack] = useState<string | null>(null);

  async function locate() {
    if (!alias) return;
    setBusy(true);
    try {
      const resp = await api.post<{ requestId: string | null; cached: boolean }>(
        `/conversations/${encodeURIComponent(alias)}/locate`
      );
      setSnack(
        resp.cached
          ? "Answered from a recent fix -- see the timeline."
          : "Location requested -- watch the timeline for the reply."
      );
    } catch (err) {
      setSnack(
        err instanceof ApiError ? String(err.detail ?? "Location request failed.") : "Location request failed."
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        size="small"
        variant="outlined"
        startIcon={<LocationOnIcon />}
        disabled={busy || !alias}
        onClick={() => void locate()}
      >
        {label}
      </Button>
      <Snackbar open={snack !== null} autoHideDuration={5000} onClose={() => setSnack(null)} message={snack} />
    </>
  );
}
