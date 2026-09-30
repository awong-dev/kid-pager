"use client";

/** `/admin/families`: superadmin table of families with member/admin/device counts.
 * Writes go through `/api/admin/families*`, `/api/admin/users/{uid}` and `/api/family/members`. */

import { useRouter } from "next/navigation";
import Alert from "@mui/material/Alert";
import Autocomplete from "@mui/material/Autocomplete";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import LaunchIcon from "@mui/icons-material/Launch";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { useFamily } from "@/lib/family-context";
import { ApiError, api } from "@/lib/api";
import type { DeviceDoc, FamilyDoc, Role, UserDoc } from "@/lib/types";

interface FamilyRow extends FamilyDoc {
  id: string;
}

interface UserRow extends UserDoc {
  uid: string;
}

interface DeviceRow extends DeviceDoc {
  id: string;
}

const emptyCreateForm = {
  name: "",
  adminAlias: "",
  adminDisplayName: "",
  adminEmail: "",
  adminPhone: "",
};

function formatCreatedAt(f: FamilyRow): string {
  return f.createdAt ? f.createdAt.toDate().toLocaleDateString() : "--";
}

function AdminFamiliesInner() {
  const router = useRouter();
  const { setFamilyId } = useFamily();


  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState(emptyCreateForm);
  const [creating, setCreating] = useState(false);

  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailName, setDetailName] = useState("");
  const [savingDetail, setSavingDetail] = useState(false);
  const [moveTarget, setMoveTarget] = useState<UserRow | null>(null);

  const detail = families.find((f) => f.id === detailId) ?? null;

  function openDetail(f: FamilyRow) {
    setDetailId(f.id);
    setDetailName(f.name);
    setMoveTarget(null);
    setError(null);
  }

  function closeDetail() {
    setDetailId(null);
  }

  async function createFamily() {
    setError(null);
    setCreating(true);
    try {
      const created = await api.post<FamilyDoc & { id: string }>("/admin/families", {
        name: createForm.name,
      });
      if (createForm.adminAlias.trim()) {
        await api.post(`/family/members?family=${encodeURIComponent(created.id)}`, {
          alias: createForm.adminAlias,
          displayName: createForm.adminDisplayName || createForm.adminAlias,
          email: createForm.adminEmail || undefined,
          phone: createForm.adminPhone || undefined,
          role: "admin",
        });
      }
      setCreateOpen(false);
      setCreateForm(emptyCreateForm);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to create family");
    } finally {
      setCreating(false);
    }
  }

  async function saveDetail() {
    setSavingDetail(true);
    setError(null);
    try {
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to update family");
    } finally {
      setSavingDetail(false);
    }
  }

  async function setRole(u: UserRow, role: Role) {
    setError(null);
    try {
      await api.patch(`/admin/users/${u.uid}`, { role });
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to change role");
    }
  }

  async function moveUserIn() {
    if (!detail || !moveTarget) return;
    setError(null);
    try {
      await api.patch(`/admin/users/${moveTarget.uid}`, { familyId: detail.id });
      setMoveTarget(null);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to move user");
    }
  }

  function openAsFamily(f: FamilyRow) {
    setFamilyId(f.id);
    router.push("/family/people");
  }

  const countsByFamily = useMemo(() => {
    const admins = new Map<string, UserRow[]>();
    const members = new Map<string, number>();
    for (const u of users) {
      if (!u.familyId) continue;
      members.set(u.familyId, (members.get(u.familyId) ?? 0) + 1);
      if (u.role === "admin") {
        const list = admins.get(u.familyId) ?? [];
        list.push(u);
        admins.set(u.familyId, list);
      }
    }
    const deviceCounts = new Map<string, number>();
    for (const d of devices) {
      if (!d.familyId) continue;
      deviceCounts.set(d.familyId, (deviceCounts.get(d.familyId) ?? 0) + 1);
    }
    return { admins, members, deviceCounts };
  }, [users, devices]);

  const detailMembers = detail ? users.filter((u) => u.familyId === detail.id) : [];
  const moveCandidates = detail ? users.filter((u) => u.familyId !== detail.id && u.kind !== "external") : [];

  const createValid = createForm.name.trim().length > 0;

  return (
    <Stack spacing={2}>
        <Typography variant="h5">Families</Typography>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}

            </TableRow>

        <DialogTitle>Create family</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Family name"
              value={createForm.name}
              onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
              fullWidth
              autoFocus
            />
            <Typography variant="caption" color="text.secondary">
              Optionally create the first family admin now (leave the alias blank to skip and add
              one later from the family detail).
            </Typography>
            <TextField
              label="Admin alias"
              value={createForm.adminAlias}
              onChange={(e) => setCreateForm({ ...createForm, adminAlias: e.target.value })}
              helperText="lowercase, e.g. mom"
              fullWidth
            />
            <TextField
              label="Admin display name"
              value={createForm.adminDisplayName}
              onChange={(e) => setCreateForm({ ...createForm, adminDisplayName: e.target.value })}
              fullWidth
            />
            <TextField
              label="Admin email"
              value={createForm.adminEmail}
              onChange={(e) => setCreateForm({ ...createForm, adminEmail: e.target.value })}
              fullWidth
            />
            <TextField
              label="Admin phone (+1XXXXXXXXXX)"
              value={createForm.adminPhone}
              onChange={(e) => setCreateForm({ ...createForm, adminPhone: e.target.value })}
              helperText="email or phone is required if creating an admin"
              fullWidth
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button onClick={() => void createFamily()} disabled={!createValid || creating}>
            Create
          </Button>
        </DialogActions>
      </Dialog>

        {detail && (
          <>
            <DialogTitle>{detail.name}</DialogTitle>
            <DialogContent>
              <Stack spacing={2} sx={{ mt: 1 }}>
                <TextField
                  label="Name"
                  value={detailName}
                  onChange={(e) => setDetailName(e.target.value)}
                  fullWidth
                />
                  <Button
                    variant="contained"
                    onClick={() => void saveDetail()}
                  >
                    Save
                  </Button>
                  <Button onClick={() => openAsFamily(detail)}>Open as family</Button>
                </Stack>

                <Divider />
                <Typography variant="subtitle2">Members</Typography>
                      </TableRow>

                <Divider />
                <Typography variant="subtitle2">Move a user in</Typography>
                  <Autocomplete
                    sx={{ flexGrow: 1 }}
                    size="small"
                    options={moveCandidates}
                    getOptionLabel={(u) => `@${u.alias} (${u.displayName})`}
                    value={moveTarget}
                    onChange={(_e, value) => setMoveTarget(value)}
                    renderInput={(params) => <TextField {...params} label="User" />}
                  />
                  <Button onClick={() => void moveUserIn()} disabled={!moveTarget}>
                    Move in
                  </Button>
                </Stack>
              </Stack>
            </DialogContent>
            <DialogActions>
              <Button onClick={closeDetail}>Close</Button>
            </DialogActions>
          </>
        )}
      </Dialog>
    </Stack>
  );
}

export default function AdminFamiliesPage() {
  return (
    <RequireAuth requireRole="super">
      <AppShell>
        <AdminFamiliesInner />
      </AppShell>
    </RequireAuth>
  );
}
