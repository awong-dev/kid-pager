"use client";

/** `/admin/hooks` (super only): message hooks via `/api/admin/hooks`. The secret is returned by
 * create and rotate-secret only and shown once. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useLive } from "@/lib/live";
import { allFamiliesQuery } from "@/lib/live/queries";
import type { FamilyDoc } from "@/lib/types";
import { responsiveTableSx } from "@/lib/tableSx";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

interface HookRow {
  id: string;
  name: string;
  url: string;
  mode: "filter" | "processor";
  families: string[] | "*";
  order: number;
  timeoutMs: number;
  failMode: "open" | "closed";
  context: number;
  enabled: boolean;
  stats: {
    calls: number;
    errors: number;
    timeouts: number;
    dropped: number;
    consecutiveErrors: number;
    lastErr: string | null;
    lastOkAt: string | null;
  };
}

interface HookWithSecret extends HookRow {
  secret: string;
}

interface Form {
  name: string;
  url: string;
  mode: "filter" | "processor";
  allFamilies: boolean;
  families: string[];
  order: string;
  timeoutMs: string;
  failMode: "open" | "closed";
  context: string;
}

const emptyForm: Form = {
  name: "",
  url: "",
  mode: "filter",
  allFamilies: true,
  families: [],
  order: "0",
  timeoutMs: "2000",
  failMode: "open",
  context: "20",
};

function toForm(h: HookRow): Form {
  return {
    name: h.name,
    url: h.url,
    mode: h.mode,
    allFamilies: h.families === "*",
    families: h.families === "*" ? [] : h.families,
    order: String(h.order),
    timeoutMs: String(h.timeoutMs),
    failMode: h.failMode,
    context: String(h.context),
  };
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function errText(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback;
  if (typeof err.detail === "string") return err.detail;
  // FastAPI 422: [{loc, msg}, ...]
  if (Array.isArray(err.detail)) {
    return err.detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? ""}`).join("; ");
  }
  return err.message;
}

function lastOk(v: string | null): string {
  if (!v) return "never";
  const t = Date.parse(v);
  return Number.isNaN(t) ? v : new Date(t).toLocaleString();
}

function AdminHooksInner() {
  const fullScreen = useFullScreenDialog();
  const [hooks, setHooks] = useState<HookRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const { docs: familyDocs } = useLive<FamilyDoc>(allFamiliesQuery());
  const familyOptions = familyDocs.map((d) => ({ id: d.id, name: d.data.name }));

  const [editId, setEditId] = useState<string | "new" | null>(null);
  const [form, setForm] = useState<Form>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null);
  const [deleting, setDeleting] = useState<HookRow | null>(null);

  const load = useCallback(async () => {
    try {
      setHooks(await api.get<HookRow[]>("/admin/hooks"));
    } catch (err) {
      setError(errText(err, "Failed to load hooks"));
    }
  }, []);

  useEffect(() => {
    // not a live collection: fetch on mount and after every write
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch
    void load();
  }, [load]);

  function openNew() {
    setForm(emptyForm);
    setFormError(null);
    setEditId("new");
  }

  function openEdit(h: HookRow) {
    setForm(toForm(h));
    setFormError(null);
    setEditId(h.id);
  }

  async function save() {
    setSaving(true);
    setFormError(null);
    const body = {
      name: form.name.trim(),
      url: form.url.trim(),
      mode: form.mode,
      families: form.allFamilies ? "*" : form.families,
      order: Number(form.order) || 0,
      timeoutMs: Number(form.timeoutMs),
      failMode: form.failMode,
      context: Number(form.context),
    };
    try {
      if (editId === "new") {
        const created = await api.post<HookWithSecret>("/admin/hooks", body);
        setSecret({ name: created.name, value: created.secret });
      } else if (editId) {
        await api.patch(`/admin/hooks/${encodeURIComponent(editId)}`, body);
      }
      setEditId(null);
      await load();
    } catch (err) {
      setFormError(errText(err, "Failed to save hook"));
    } finally {
      setSaving(false);
    }
  }

  async function toggle(h: HookRow, enabled: boolean) {
    setError(null);
    try {
      await api.patch(`/admin/hooks/${encodeURIComponent(h.id)}`, { enabled });
      await load();
    } catch (err) {
      setError(errText(err, "Failed to update hook"));
    }
  }

  async function rotate() {
    if (!editId || editId === "new") return;
    setFormError(null);
    try {
      const r = await api.post<HookWithSecret>(`/admin/hooks/${encodeURIComponent(editId)}/rotate-secret`);
      setSecret({ name: r.name, value: r.secret });
      setEditId(null);
      await load();
    } catch (err) {
      setFormError(errText(err, "Failed to rotate secret"));
    }
  }

  async function remove() {
    if (!deleting) return;
    try {
      await api.del(`/admin/hooks/${encodeURIComponent(deleting.id)}`);
      setDeleting(null);
      setEditId(null);
      await load();
    } catch (err) {
      setError(errText(err, "Failed to delete hook"));
      setDeleting(null);
    }
  }

  const isFilter = form.mode === "filter";
  const valid =
    form.name.trim().length > 0 &&
    /^https?:\/\/\S+$/.test(form.url.trim()) &&
    (form.allFamilies || form.families.length > 0);

  return (
    <Stack spacing={2}>
      <Stack direction="row" sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h5">Hooks</Typography>
        <Button variant="contained" onClick={openNew}>
          Add hook
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary">
        Hooks receive every message in the families they cover. Each family sees which hooks cover it.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}

      <TableContainer sx={responsiveTableSx([2, 3, 4])}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Name</TableCell>
              <TableCell>Mode / host</TableCell>
              <TableCell>Families</TableCell>
              <TableCell>Order</TableCell>
              <TableCell>Stats</TableCell>
              <TableCell>Enabled</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {hooks.map((h) => (
              <TableRow key={h.id} hover sx={{ cursor: "pointer" }} onClick={() => openEdit(h)}>
                <TableCell>{h.name}</TableCell>
                <TableCell>
                  <Chip size="small" label={h.mode} sx={{ mr: 0.5 }} /> {hostOf(h.url)}
                </TableCell>
                <TableCell>{h.families === "*" ? "all" : h.families.join(", ")}</TableCell>
                <TableCell>{h.order}</TableCell>
                <TableCell>
                  <Typography variant="caption" component="div">
                    {h.stats.calls} calls, {h.stats.errors} errors, {h.stats.timeouts} timeouts
                  </Typography>
                  <Typography variant="caption" component="div" color={h.stats.lastErr ? "error" : "text.secondary"}>
                    last error: {h.stats.lastErr ?? "none"}
                  </Typography>
                  <Typography variant="caption" component="div" color="text.secondary">
                    last ok: {lastOk(h.stats.lastOkAt)}
                  </Typography>
                </TableCell>
                <TableCell onClick={(e) => e.stopPropagation()}>
                  <Switch
                    checked={h.enabled}
                    onChange={(e) => void toggle(h, e.target.checked)}
                    slotProps={{ input: { "aria-label": `enable ${h.name}` } }}
                  />
                </TableCell>
              </TableRow>
            ))}
            {hooks.length === 0 && (
              <TableRow>
                <TableCell colSpan={6}>
                  <Typography variant="body2" color="text.secondary">
                    No hooks yet.
                  </Typography>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>

      <Dialog fullScreen={fullScreen} open={editId !== null} onClose={() => setEditId(null)} fullWidth maxWidth="sm">
        <DialogTitle>{editId === "new" ? "Add hook" : "Edit hook"}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField label="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} fullWidth />
            <TextField
              label="URL"
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              helperText="http(s)://host[:port]/path"
              fullWidth
            />
            <TextField
              select
              label="Mode"
              value={form.mode}
              onChange={(e) => setForm({ ...form, mode: e.target.value as Form["mode"] })}
              helperText={isFilter ? "Runs before delivery and can block or change a message" : "Receives each message after it is stored"}
            >
              <MenuItem value="filter">filter</MenuItem>
              <MenuItem value="processor">processor</MenuItem>
            </TextField>
            <TextField
              select
              label="Families"
              value={form.allFamilies ? "all" : "chosen"}
              onChange={(e) => setForm({ ...form, allFamilies: e.target.value === "all" })}
            >
              <MenuItem value="all">All families</MenuItem>
              <MenuItem value="chosen">Chosen families</MenuItem>
            </TextField>
            {!form.allFamilies && (
              <TextField
                select
                label="Chosen families"
                value={form.families}
                onChange={(e) => {
                  const v = e.target.value as unknown as string[] | string;
                  setForm({ ...form, families: typeof v === "string" ? v.split(",") : v });
                }}
                slotProps={{ select: { multiple: true } }}
              >
                {familyOptions.map((f) => (
                  <MenuItem key={f.id} value={f.id}>
                    {f.name}
                  </MenuItem>
                ))}
              </TextField>
            )}
            <Stack direction="row" spacing={1}>
              <TextField
                label="Order"
                type="number"
                value={form.order}
                onChange={(e) => setForm({ ...form, order: e.target.value })}
                helperText="Lower runs first"
                fullWidth
              />
              <TextField
                label="Context messages"
                type="number"
                value={form.context}
                onChange={(e) => setForm({ ...form, context: e.target.value })}
                helperText="0 to 50"
                fullWidth
              />
            </Stack>
            {isFilter && (
              <Stack direction="row" spacing={1}>
                <TextField
                  label="Timeout (ms)"
                  type="number"
                  value={form.timeoutMs}
                  onChange={(e) => setForm({ ...form, timeoutMs: e.target.value })}
                  helperText="100 to 10000"
                  fullWidth
                />
                <TextField
                  select
                  label="If the hook fails"
                  value={form.failMode}
                  onChange={(e) => setForm({ ...form, failMode: e.target.value as Form["failMode"] })}
                  fullWidth
                >
                  <MenuItem value="open">open (let the message through)</MenuItem>
                  <MenuItem value="closed">closed (block the message)</MenuItem>
                </TextField>
              </Stack>
            )}
            {formError && <Alert severity="error">{formError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          {editId && editId !== "new" && (
            <>
              <Button color="error" onClick={() => setDeleting(hooks.find((h) => h.id === editId) ?? null)}>
                Delete
              </Button>
              <Button onClick={() => void rotate()}>Rotate secret</Button>
            </>
          )}
          <Button onClick={() => setEditId(null)}>Cancel</Button>
          <Button variant="contained" onClick={() => void save()} disabled={!valid || saving}>
            Save
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={secret !== null} onClose={() => setSecret(null)} fullWidth maxWidth="xs">
        <DialogTitle>Secret for {secret?.name}</DialogTitle>
        <DialogContent>
          <Stack spacing={1.5} sx={{ mt: 1 }}>
            <Alert severity="warning">This secret cannot be shown again. Copy it now and give it to the hook.</Alert>
            <Stack direction="row" sx={{ alignItems: "center" }}>
              <Typography sx={{ fontFamily: "monospace", wordBreak: "break-all" }}>{secret?.value}</Typography>
              <IconButton aria-label="copy secret" onClick={() => void navigator.clipboard?.writeText(secret?.value ?? "")}>
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSecret(null)}>Done</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={deleting !== null} onClose={() => setDeleting(null)}>
        <DialogTitle>Delete {deleting?.name}?</DialogTitle>
        <DialogContent>
          <Typography>The hook stops receiving messages immediately. This cannot be undone.</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleting(null)}>Cancel</Button>
          <Button color="error" onClick={() => void remove()}>
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

export default function AdminHooksPage() {
  return (
    <RequireAuth requireRole="super">
      <AppShell>
        <AdminHooksInner />
      </AppShell>
    </RequireAuth>
  );
}
