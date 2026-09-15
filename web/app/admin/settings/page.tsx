"use client";


import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
  return (
  );
}

function AdminSettingsInner() {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    });
  }, []);

  async function save() {
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      setStatus("Saved.");
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack spacing={3} sx={{ maxWidth: 480 }}>
      <Typography variant="h5">Retention</Typography>


      <Alert severity="info">
      </Alert>

      {status && <Alert severity="success">{status}</Alert>}
      {error && <Alert severity="error">{error}</Alert>}

        {saving ? "Saving..." : "Save"}
      </Button>
    </Stack>
  );
}

export default function AdminSettingsPage() {
  return (
    <RequireAuth requireAdmin>
      <AppShell>
        <AdminSettingsInner />
      </AppShell>
    </RequireAuth>
  );
}
