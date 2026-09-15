"use client";

/** `/settings/backends`: list, add (Google Chat link code) and enable backends. Reads
 * `users/{uid}/backends`; writes use `/api/me/backends*`. A `gchat` backend starts disabled. */

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Typography from "@mui/material/Typography";
import DeleteIcon from "@mui/icons-material/Delete";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { BackendDoc } from "@/lib/types";

interface BackendRow extends BackendDoc {
  id: string;
}

function configSummary(b: BackendRow): string {
  if (b.kind === "gchat") return typeof b.config.space === "string" ? b.config.space : "not linked";
  if (b.kind === "pager") return typeof b.config.deviceId === "string" ? b.config.deviceId : "";
  return "";
}

function BackendsInner() {
  const { me } = useAuth();
  const [error, setError] = useState<string | null>(null);

  async function toggleEnabled(b: BackendRow) {
    setError(null);
    try {
      await api.patch(`/me/backends/${b.id}`, { enabled: !b.enabled });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to update");
    }
  }

  async function removeBackend(b: BackendRow) {
    setError(null);
    try {
      await api.del(`/me/backends/${b.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to remove");
    }
  }

  async function addGchat() {
    setError(null);
    try {
      await api.post<BackendRow>("/me/backends", { kind: "gchat", config: {}, enabled: true });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to add Google Chat backend");
    }
  }

  return (
    <Stack spacing={2}>
      <Typography variant="h5">Backends</Typography>
      {error && <Alert severity="error">{error}</Alert>}

      <Stack spacing={1.5}>
        {backends.map((b) => (
          <Card key={b.id} variant="outlined">
            <CardContent>
                <Chip label={b.kind} size="small" />
                <Typography sx={{ flexGrow: 1 }}>{configSummary(b)}</Typography>
                {!b.verifiedAt && b.kind !== "webapp" && b.kind !== "pager" && (
                  <Chip label="unverified" color="warning" size="small" />
                )}
                <Switch checked={b.enabled} onChange={() => void toggleEnabled(b)} />
                {b.kind !== "pager" && b.kind !== "webapp" && (
                  <IconButton onClick={() => void removeBackend(b)} aria-label="remove">
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                )}
              </Stack>
            </CardContent>
          </Card>
        ))}
      </Stack>

        <Button variant="outlined" onClick={() => void addGchat()}>
          Add Google Chat
        </Button>
      </Stack>

      <Box>
        <Alert severity="info">
        </Alert>
      </Box>
    </Stack>
  );
}

export default function BackendsPage() {
  return (
    <RequireAuth>
      <AppShell>
        <BackendsInner />
      </AppShell>
    </RequireAuth>
  );
}
