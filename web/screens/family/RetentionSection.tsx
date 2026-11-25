"use client";

/** "Message history" on `/family/settings`: retention days per data class (bounds mirror
 * `relay/app/store/settings.py`) and the last sweep's `settings/meta.lastSweep`. The retention API
 * (`PUT /api/admin/settings`) is superadmin-only and server-wide, not per family, so a family admin
 * sees the values read-only. */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import SectionBlock from "@/components/SectionBlock";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { subscribeLive, useLive } from "@/lib/live";
import { metaSettingsQuery, retentionSettingsQuery } from "@/lib/live/queries";
import { locationsDaysOf } from "@/lib/location";
import { tsToMillis } from "@/lib/time";
import type { MetaSettingsDoc, RetentionSettingsDoc } from "@/lib/types";

type DayKey = "messagesDays" | "locationsDays" | "alertsDays" | "batteryDays";

const FIELDS: { key: DayKey; label: string; min: number; max: number }[] = [
  { key: "messagesDays", label: "Messages", min: 1, max: 365 },
  { key: "locationsDays", label: "Locations", min: 1, max: 30 },
  { key: "alertsDays", label: "Alerts", min: 7, max: 365 },
  { key: "batteryDays", label: "Battery", min: 7, max: 365 },
];

const DEFAULTS: Record<DayKey, number> = { messagesDays: 28, locationsDays: 7, alertsDays: 90, batteryDays: 90 };
const DAY_MS = 24 * 60 * 60 * 1000;

function fromDoc(doc: RetentionSettingsDoc): Record<DayKey, number> {
  const legacyMessages = doc.messages ? doc.messages.n * (doc.messages.unit === "weeks" ? 7 : 1) : undefined;
  return {
    messagesDays: doc.messagesDays ?? legacyMessages ?? DEFAULTS.messagesDays,
    locationsDays: locationsDaysOf(doc),
    alertsDays: doc.alertsDays ?? DEFAULTS.alertsDays,
    batteryDays: doc.batteryDays ?? DEFAULTS.batteryDays,
  };
}

function fmt(ms: number | null): string {
  return ms === null ? "none" : new Date(ms).toLocaleString();
}

function LastRunPanel({ meta, locationsDays }: { meta: MetaSettingsDoc | undefined; locationsDays: number }) {
  const sweep = meta?.lastSweep;
  if (!sweep) {
    return (
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle1">Last run</Typography>
        <Typography color="text.secondary">The sweep has not recorded a run yet.</Typography>
      </Paper>
    );
  }
  const atMs = tsToMillis(sweep.at);
  const oldestMsg = tsToMillis(sweep.oldest?.messages);
  const oldestLoc = tsToMillis(sweep.oldest?.locations);
  const counts = Object.entries(sweep.counts ?? {});
  // a location older than the limit plus a day means the sweep is not keeping up
  const locOverdue = oldestLoc !== null && atMs !== null && atMs - oldestLoc > (locationsDays + 1) * DAY_MS;
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack spacing={0.5}>
        <Typography variant="subtitle1">Last run</Typography>
        <Typography>{fmt(atMs)}</Typography>
        {counts.length > 0 && (
          <Typography variant="body2" color="text.secondary">
            Deleted: {counts.map(([k, v]) => `${k} ${v}`).join(", ")}
          </Typography>
        )}
        <Typography variant="body2">Oldest surviving message: {fmt(oldestMsg)}</Typography>
        <Typography variant="body2" color={locOverdue ? "error" : undefined}>
          Oldest surviving location: {fmt(oldestLoc)}
          {locOverdue ? ` -- older than the ${locationsDays}-day limit plus a day` : ""}
        </Typography>
      </Stack>
    </Paper>
  );
}

export default function RetentionSection() {
  const { isSuper } = useAuth();
  const [values, setValues] = useState<Record<DayKey, number>>(DEFAULTS);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const { docs: metaDocs } = useLive<MetaSettingsDoc>(metaSettingsQuery());

  useEffect(() => {
    return subscribeLive<RetentionSettingsDoc>(retentionSettingsQuery(), (docs) => {
      if (docs[0]) setValues(fromDoc(docs[0].data));
    });
  }, []);

  const invalid = FIELDS.some((f) => !Number.isInteger(values[f.key]) || values[f.key] < f.min || values[f.key] > f.max);

  async function save() {
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      await api.put("/admin/settings", values);
      setStatus("Saved.");
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  }

  return (
    <SectionBlock
      id="history"
      title="Message history"
      description="How long messages, locations, alerts and battery readings are kept before they are deleted."
    >
      <Alert severity={isSuper ? "info" : "warning"}>
        {isSuper
          ? "These limits apply to the whole server, not just one family."
          : "These limits apply to the whole server, not just your family, so only the server administrator can change them."}
      </Alert>

      {FIELDS.map((f) => (
        <Stack key={f.key} direction="row" spacing={2} sx={{ alignItems: "center" }}>
          <Typography sx={{ minWidth: 100 }}>{f.label}</Typography>
          <TextField
            type="number"
            label="days"
            size="small"
            value={Number.isNaN(values[f.key]) ? "" : values[f.key]}
            error={!Number.isInteger(values[f.key]) || values[f.key] < f.min || values[f.key] > f.max}
            helperText={`${f.min} to ${f.max}`}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value === "" ? NaN : Number(e.target.value) })}
            disabled={!isSuper}
            sx={{ width: 120 }}
            slotProps={{ htmlInput: { min: f.min, max: f.max } }}
          />
        </Stack>
      ))}

      <Alert severity="info">
        Location history cannot be kept longer than 30 days. Locations are cleaned up hourly; messages
        and the other classes daily, so a record can outlive its limit briefly before it is deleted.
      </Alert>

      <LastRunPanel meta={metaDocs[0]?.data} locationsDays={values.locationsDays} />

      {status && <Alert severity="success">{status}</Alert>}
      {error && <Alert severity="error">{error}</Alert>}

      <Button variant="contained" disabled={!isSuper || saving || invalid} onClick={() => void save()} sx={{ alignSelf: "flex-start" }}>
        {saving ? "Saving..." : "Save"}
      </Button>
    </SectionBlock>
  );
}
