"use client";

/** `/family/alerts`: inbox of `families/{fam}/alerts`, newest first; "Show handled" adds
 * `handled`/`dismissed` ones. Each row is an `AlertCard` with its own actions. */

import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Typography from "@mui/material/Typography";

import AlertCard, { type AlertRow } from "@/components/AlertCard";
import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { useFamily } from "@/lib/family-context";
import type { AlertDoc } from "@/lib/types";

function FamilyAlertsInner() {
  const { familyId } = useFamily();
  const [showHandled, setShowHandled] = useState(false);


  return (
    <Stack spacing={2}>
        <Typography variant="h5">Alerts</Typography>
        <FormControlLabel
          control={<Switch checked={showHandled} onChange={(e) => setShowHandled(e.target.checked)} />}
          label="Show handled"
        />
      </Stack>

      {alerts.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {showHandled ? "No alerts yet." : "No open alerts."}
        </Typography>
      )}

      <Stack spacing={1.5}>
        {alerts.map((a) => (
          <AlertCard key={a.id} alert={a} />
        ))}
      </Stack>
    </Stack>
  );
}

export default function FamilyAlertsPage() {
  return (
    <RequireAuth requireRole="admin">
      <AppShell>
        <FamilyAlertsInner />
      </AppShell>
    </RequireAuth>
  );
}
