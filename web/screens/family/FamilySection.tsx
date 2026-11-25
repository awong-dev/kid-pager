"use client";

/** "Family" on `/family/settings`: the family's name (`PATCH /api/family`), the family-level
 * switches (`FamilySettingsSection`) and the hooks that also receive its messages. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import FamilySettingsSection from "@/components/FamilySettingsSection";
import SectionBlock from "@/components/SectionBlock";
import { ApiError, api } from "@/lib/api";
import { familyQuery, useFamily } from "@/lib/family-context";
import type { FamilyDoc } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

export default function FamilySection() {
  const fullScreen = useFullScreenDialog();
  const { familyId } = useFamily();
  const [fam, setFam] = useState<FamilyDoc | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setFam(await api.get<FamilyDoc>(`/family${familyQuery()}`));
      setLoadError(false);
    } catch {
      setLoadError(true);
    }
  }, []);

  useEffect(() => {
    if (!familyId) return;
    void (async () => {
      await load();
    })();
  }, [familyId, load]);

  async function saveRename() {
    setRenameError(null);
    try {
      await api.patch(`/family${familyQuery()}`, { name: renameValue.trim() });
      setRenameOpen(false);
      await load();
    } catch (err) {
      setRenameError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to rename family");
    }
  }

  const trimmed = renameValue.trim();
  const valid = trimmed.length >= 1 && [...trimmed].length <= 40;

  return (
    <SectionBlock id="family" title="Family" description="Your family's name and how the pagers and the bridge phone behave.">
      {loadError && <Alert severity="error">Could not load your family&apos;s settings. Try reloading the page.</Alert>}
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
        <Typography variant="body1">Family name: {fam?.name ?? "--"}</Typography>
        <Button
          size="small"
          disabled={!fam}
          onClick={() => {
            setRenameValue(fam?.name ?? "");
            setRenameError(null);
            setRenameOpen(true);
          }}
        >
          Rename
        </Button>
      </Stack>

      <FamilySettingsSection settings={fam?.settings} onSaved={(f) => setFam((prev) => (prev ? { ...prev, settings: f.settings } : prev))} />

      {(fam?.hooks ?? []).map((h) => (
        <Typography key={`${h.name}@${h.host}`} variant="body2" color="text.secondary">
          Messages in this family are also sent to: {h.name} ({h.host})
        </Typography>
      ))}

      <Dialog fullScreen={fullScreen} open={renameOpen} onClose={() => setRenameOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Rename family</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              autoFocus
              label="Family name"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              helperText="1-40 characters"
              fullWidth
            />
            {renameError && <Alert severity="error">{renameError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRenameOpen(false)}>Cancel</Button>
          <Button onClick={() => void saveRename()} disabled={!valid}>
            Save
          </Button>
        </DialogActions>
      </Dialog>
    </SectionBlock>
  );
}
