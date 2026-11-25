"use client";

/** Family-level switches: pager system notices, chat link checks, SMS top-up policy. */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import FormControlLabel from "@mui/material/FormControlLabel";
import FormHelperText from "@mui/material/FormHelperText";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";

import { ApiError, api } from "@/lib/api";
import { familyQuery } from "@/lib/family-context";
import type { FamilyDoc, FamilySettingsDoc } from "@/lib/types";

export const DEFAULT_FAMILY_SETTINGS: FamilySettingsDoc = {
  notices: { pager: true },
  linkChecks: true,
  topup: { mode: "off", perMonth: 1 },
};

type Patch = {
  notices?: { pager?: boolean };
  linkChecks?: boolean;
  topup?: { mode?: FamilySettingsDoc["topup"]["mode"]; perMonth?: number };
};

const TOPUP_OPTIONS = ["off", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "unlimited"];

function topupValue(t: FamilySettingsDoc["topup"]): string {
  return t.mode === "limited" ? String(t.perMonth) : t.mode;
}

export default function FamilySettingsSection({
  settings,
  onSaved,
}: {
  settings: FamilySettingsDoc | undefined;
  onSaved: (fam: FamilyDoc) => void;
}) {
  const s = settings ?? DEFAULT_FAMILY_SETTINGS;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(patch: Patch) {
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.patch<FamilyDoc>(`/family${familyQuery()}`, { settings: patch }));
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save family settings");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack spacing={1}>
      {error && <Alert severity="error">{error}</Alert>}
      <FormControlLabel
        control={
          <Switch
            checked={s.notices.pager}
            disabled={busy}
            onChange={(e) => void save({ notices: { pager: e.target.checked } })}
          />
        }
        label="Send system notices to pagers"
      />
      <div>
        <FormControlLabel
          control={
            <Switch
              checked={s.linkChecks}
              disabled={busy}
              onChange={(e) => void save({ linkChecks: e.target.checked })}
            />
          }
          label="Check chat links on the bridge phone"
        />
        <FormHelperText sx={{ mt: -0.5, ml: 6 }}>Briefly lights the phone screen.</FormHelperText>
      </div>
      <TextField
        select
        size="small"
        label="SMS top-ups"
        value={topupValue(s.topup)}
        disabled={busy}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "off" || v === "unlimited") void save({ topup: { mode: v } });
          else void save({ topup: { mode: "limited", perMonth: Number(v) } });
        }}
        helperText="Lets the bridge phone buy more texts on the carrier account when it runs low."
        sx={{ maxWidth: 280 }}
      >
        {TOPUP_OPTIONS.map((o) => (
          <MenuItem key={o} value={o}>
            {o === "off" ? "Off" : o === "unlimited" ? "Unlimited" : `${o} per month`}
          </MenuItem>
        ))}
      </TextField>
    </Stack>
  );
}
