"use client";

/** One person's address book: the entries on their pager (and the ones only the web can reach),
 * why each is there or not sendable, their own nicknames, and Remove for entries added from the
 * pager. Used by `/account/book` (the viewer's own) and by the Permissions page ("On their pager").
 * Membership itself is edited by the approved list and the messaging rules, not here. */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import EditIcon from "@mui/icons-material/Edit";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import ListSubheader from "@mui/material/ListSubheader";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import BookNickDialog from "@/components/BookNickDialog";
import { formatPhoneDigits } from "@/components/NewChatDialog";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  BOOK_GROUP_ORDER,
  type BookEntry,
  type BookResponse,
  bookGroup,
  reasonText,
  removeAdded,
  useBook,
} from "@/lib/book";
import { sourceLabel } from "@/lib/bridges";
import { fedRemoteName } from "@/lib/federation";

export default function MemberAddressBook({ ownerUid }: { ownerUid: string }) {
  const { me } = useAuth();
  const { data, error, loading, reload } = useBook(ownerUid === me?.uid ? undefined : ownerUid);
  const [editing, setEditing] = useState<BookEntry | null>(null);
  // The DELETE response is the refreshed book; it wins until the next reload.
  const [local, setLocal] = useState<{ owner: string; data: BookResponse } | null>(null);
  const [removing, setRemoving] = useState<BookEntry | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const shown = local && local.owner === ownerUid ? local.data : data;

  async function confirmRemove() {
    if (!removing?.uid) return;
    try {
      setLocal({ owner: ownerUid, data: await removeAdded(ownerUid, removing.uid) });
      setRemoveError(null);
    } catch (err) {
      setRemoveError(err instanceof ApiError ? String(err.detail ?? err.message) : "Could not remove the entry");
    }
    setRemoving(null);
  }

  const grouped = useMemo(() => {
    const out = new Map<string, BookEntry[]>();
    for (const g of BOOK_GROUP_ORDER) out.set(g, []);
    for (const e of shown?.entries ?? []) out.get(bookGroup(e))?.push(e);
    for (const rows of out.values()) rows.sort((a, b) => a.label.localeCompare(b.label));
    return out;
  }, [shown]);

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}
      {removeError && <Alert severity="error">{removeError}</Alert>}
      {loading && !shown && <CircularProgress size={24} />}

      {shown?.truncated && (
        <Alert severity="info">
          This pager shows {shown.pagerCap} of {shown.entries.length} entries. Entries marked Not on pager are
          only reachable from the web.
        </Alert>
      )}

      {shown && shown.entries.length === 0 && <Alert severity="info">This address book is empty.</Alert>}

      {BOOK_GROUP_ORDER.map((g) => {
        const rows = grouped.get(g) ?? [];
        if (rows.length === 0) return null;
        return (
          <List key={g} dense subheader={<ListSubheader disableSticky>{g}</ListSubheader>}>
            {rows.map((e) => {
              const secondary = [
                e.kind === "external" && e.phone
                  ? formatPhoneDigits(e.phone)
                  : e.fed
                    ? fedRemoteName(e.fed)
                    : e.kind === "group" || e.chat
                      ? ""
                      : `@${e.alias}`,
                e.nick ? `was ${e.displayName}` : "",
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <ListItem
                  key={`${e.kind}:${e.alias}`}
                  secondaryAction={
                    e.kind !== "group" && e.uid ? (
                      <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
                        {e.added && (
                          <Button size="small" color="error" onClick={() => setRemoving(e)}>
                            Remove
                          </Button>
                        )}
                        <IconButton edge="end" aria-label={`Edit nickname for ${e.displayName}`} onClick={() => setEditing(e)}>
                          <EditIcon fontSize="small" />
                        </IconButton>
                      </Stack>
                    ) : null
                  }
                >
                  <ListItemText primary={e.label} secondary={secondary || undefined} />
                  {e.chat && (
                    <Chip
                      size="small"
                      variant="outlined"
                      label={sourceLabel(e.chat.source)}
                      sx={{ mr: e.sendable && e.onPager ? 4 : 1 }}
                    />
                  )}
                  {e.added && <Chip size="small" variant="outlined" label="Added from pager" sx={{ mr: 1 }} />}
                  {!e.sendable && <Chip size="small" label={reasonText(e.reason)} sx={{ mr: 4 }} />}
                  {e.sendable && !e.onPager && (
                    <Chip size="small" color="warning" variant="outlined" label="Not on pager" sx={{ mr: 4 }} />
                  )}
                </ListItem>
              );
            })}
          </List>
        );
      })}

      <BookNickDialog
        key={editing?.alias ?? "none"}
        owner={ownerUid}
        entry={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setLocal(null);
          reload();
        }}
      />

      <Dialog open={!!removing} onClose={() => setRemoving(null)} fullWidth maxWidth="xs">
        <DialogTitle>Remove from address book</DialogTitle>
        <DialogContent>
          <Typography>
            Remove {removing?.label}? It was added from the pager; it disappears from the list unless
            another rule already includes it.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRemoving(null)}>Cancel</Button>
          <Button color="error" onClick={() => void confirmRemove()}>
            Remove
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
