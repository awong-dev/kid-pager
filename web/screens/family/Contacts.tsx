"use client";

/** `/family/contacts`: the family's SMS contacts with add/rename/delete via `/api/family/contacts`,
 * plus a read-only "Linked families" list. A duplicate name is a 409 shown in the dialog. */

import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import FedAddContactDialog from "@/components/FedAddContactDialog";
import { ApiError, api } from "@/lib/api";
import type { BookResponse } from "@/lib/book";
import { useDirectory } from "@/lib/directory";
import { fedRemoteHost, fedRemoteName, getFamilyFed, type FedLink, type FedRef } from "@/lib/federation";
import { familyQuery, useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { allowEdgesOfFamilyQuery } from "@/lib/live/queries";
import type { AllowEdgeDoc, FamilyDoc } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";
import { responsiveTableSx } from "@/lib/tableSx";

interface ContactRow {
  uid: string;
  alias: string;
  phone: string;
  displayName: string;
  approvedFor: string[];
  impliedFor?: string[];
  // A person on a peer server; `sendable`/`reason` are the peer-state verdict (null otherwise).
  fed?: FedRef | null;
  sendable?: boolean | null;
  reason?: string | null;
}

interface LinkedEdge {
  key: string;
  fromUid: string;
  toUid: string;
}

const NAME_HELP = "Must be unique in the family; the pager shows the first 16 characters.";

const emptyAdd = { phone: "", name: "" };

function FamilyContactsInner() {
  const fullScreen = useFullScreenDialog();
  const { familyId } = useFamily();
  const { byUid, contacts: people } = useDirectory();
  // contact uid -> member uids whose address book marks it "added from the pager".
  const [addedBy, setAddedBy] = useState<Record<string, string[]>>({});
  // contact uid -> its server and whether that peering is gone, from the same books.
  const [fedLinks, setFedLinks] = useState<FedLink[]>([]);
  const [fedOn, setFedOn] = useState<boolean | undefined>(undefined);
  const [fedOpen, setFedOpen] = useState(false);
  const [contacts, setContacts] = useState<ContactRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addForm, setAddForm] = useState(emptyAdd);
  const [renaming, setRenaming] = useState<ContactRow | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ContactRow | null>(null);

  async function loadContacts() {
    setLoadError(null);
    try {
      // returns a bare list
      const resp = await api.get<ContactRow[]>(`/family/contacts${familyQuery()}`);
      setContacts(Array.isArray(resp) ? resp : []);
    } catch (err) {
      setLoadError(
        err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to load contacts"
      );
    }
  }

  useEffect(() => {
    (async () => {
      await loadContacts();
    })();
  }, [familyId]);

  // `GET /api/family` says whether federation is on; when off, skip the links request (404).
  useEffect(() => {
    let live = true;
    api
      .get<FamilyDoc>(`/family${familyQuery()}`)
      .catch(() => null)
      .then((fam) => {
        if (!live) return;
        const flag = fam?.federation;
        setFedOn(flag);
        if (flag !== true) {
          setFedLinks([]);
          return;
        }
        getFamilyFed()
          .then((f) => live && setFedLinks(f.links))
          .catch(() => live && setFedLinks([]));
      });
    return () => {
      live = false;
    };
  }, [familyId]);

  // The contacts API does not say who added a contact: read each member's book
  // (`GET /api/book?uid=`) and collect the `added` marks.
  useEffect(() => {
    if (!familyId) return;
    let cancelled = false;
    const members = people.filter((p) => byUid(p.uid)?.familyId === familyId);
    Promise.all(
      members.map((m) =>
        api
          .get<BookResponse>(`/book?uid=${encodeURIComponent(m.uid)}`)
          .then((b) => ({
            added: b.entries.filter((e) => e.added && e.uid).map((e) => [e.uid as string, m.uid] as const),
          }))
          .catch(() => ({ added: [] as (readonly [string, string])[] }))
      )
    ).then((rows) => {
      if (cancelled) return;
      const out: Record<string, string[]> = {};
      for (const [peer, owner] of rows.flatMap((r) => r.added)) (out[peer] ??= []).push(owner);
      setAddedBy(out);
    });
    return () => {
      cancelled = true;
    };
  }, [familyId, people, byUid, contacts]);

  // Linked families: cross-family `allow` edges, i.e. `familyIds` with two distinct entries.
  const { docs: edgeDocs } = useLive<AllowEdgeDoc>(familyId ? allowEdgesOfFamilyQuery(familyId) : null);
  const linkedEdges = useMemo(() => {
    const rows: LinkedEdge[] = [];
    for (const d of edgeDocs) {
      const ids = d.data.familyIds ?? [];
      if (new Set(ids).size === 2) {
        rows.push({ key: d.id, fromUid: d.data.fromUid, toUid: d.data.toUid });
      }
    }
    return rows;
  }, [edgeDocs]);

  function labelFor(uid: string): string {
    const entry = byUid(uid);
    return entry ? `@${entry.alias}` : uid;
  }

  const addValid = addForm.phone.trim() && addForm.name.trim();

  async function addContact() {
    setError(null);
    setDialogError(null);
    try {
      await api.post(`/family/contacts${familyQuery()}`, {
        phone: addForm.phone.trim(),
        name: addForm.name.trim(),
      });
      setAddOpen(false);
      setAddForm(emptyAdd);
      await loadContacts();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setDialogError(String(err.detail ?? err.message));
      } else {
        setAddOpen(false);
        setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to add contact");
      }
    }
  }

  function openRename(c: ContactRow) {
    setError(null);
    setDialogError(null);
    setRenaming(c);
    setRenameValue(c.displayName);
  }

  async function saveRename() {
    if (!renaming) return;
    setError(null);
    setDialogError(null);
    try {
      await api.patch(`/family/contacts/${renaming.uid}${familyQuery()}`, { name: renameValue.trim() });
      setRenaming(null);
      await loadContacts();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setDialogError(String(err.detail ?? err.message));
      } else {
        setRenaming(null);
        setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to rename contact");
      }
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    setError(null);
    try {
      await api.del(`/family/contacts/${deleting.uid}${familyQuery()}`);
      setDeleting(null);
      await loadContacts();
    } catch (err) {
      setDeleting(null);
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to delete contact");
    }
  }

  return (
    <Stack spacing={3}>
      <Stack direction="row" sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h5">Contacts</Typography>
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
          {fedLinks.some((l) => l.state === "active") && (
            <Button variant="outlined" onClick={() => setFedOpen(true)}>
              Add from another family
            </Button>
          )}
          <Button
            variant="contained"
            onClick={() => {
              setDialogError(null);
              setAddOpen(true);
            }}
          >
            Add contact
          </Button>
        </Stack>
      </Stack>
      <Typography variant="body2" color="text.secondary">
        Contacts are texted by the pager&apos;s own SMS. Approve a contact for a member (People → Approved) to
        put it on their pager. Members with an SMS number text contacts through the relay; others use the
        pager&apos;s own SIM.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}
      {loadError && <Alert severity="error">{loadError}</Alert>}

      <TableContainer sx={responsiveTableSx([3])}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Name</TableCell>
              <TableCell>Number</TableCell>
              <TableCell>Approved for</TableCell>
              <TableCell align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {contacts.map((c) => (
              <TableRow
                key={c.uid}
                hover
                sx={c.fed && (fedOn === false || c.reason === "fed_off" || c.reason === "fed_reauth") ? { opacity: 0.55 } : undefined}
              >
                <TableCell>{c.displayName}</TableCell>
                <TableCell>
                  {c.phone ||
                    (c.fed ? (
                      <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap", alignItems: "center" }}>
                        <Typography variant="body2">
                          {fedRemoteName(c.fed)}
                          {fedRemoteHost(c.fed) && (
                            <Typography component="span" variant="caption" color="text.secondary">
                              {" "}
                              {fedRemoteHost(c.fed)}
                            </Typography>
                          )}
                        </Typography>
                        {(fedOn === false || c.reason === "fed_off") && (
                          <Chip size="small" variant="outlined" label="Federation is off" />
                        )}
                        {fedOn !== false && c.reason === "fed_reauth" && (
                          <Chip size="small" color="warning" variant="outlined" label="Needs re-authorizing" />
                        )}
                        {fedOn !== false && c.sendable === false && c.reason === "unpeered" && (
                          <Tooltip title="This family is not linked or the link is suspended, so nothing can be sent to this person">
                            <Chip size="small" color="warning" variant="outlined" label="unlinked" />
                          </Tooltip>
                        )}
                      </Stack>
                    ) : (
                      ""
                    ))}
                </TableCell>
                <TableCell>
                  <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap" }}>
                    {(c.approvedFor ?? []).map((uid) => (
                      <Chip key={uid} size="small" label={labelFor(uid)} />
                    ))}
                    {(c.impliedFor ?? []).map((uid) => (
                      <Tooltip key={`i-${uid}`} title="On their pager because their policy is Open">
                        <Chip size="small" variant="outlined" label={`${labelFor(uid)} (policy)`} />
                      </Tooltip>
                    ))}
                    {(addedBy[c.uid] ?? []).map((uid) => (
                      <Chip key={`a-${uid}`} size="small" color="info" variant="outlined" label={`added by ${labelFor(uid)}`} />
                    ))}
                  </Stack>
                </TableCell>
                <TableCell align="right">
                  <Button size="small" onClick={() => openRename(c)}>
                    Rename
                  </Button>
                  <Button size="small" color="error" onClick={() => setDeleting(c)}>
                    Delete
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Stack spacing={1}>
        <Typography variant="h6">Linked families</Typography>
        <Typography variant="body2" color="text.secondary">
          Ask your superadmin to link a person from another family.
        </Typography>
        {linkedEdges.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No cross-family links yet.
          </Typography>
        ) : (
          <TableContainer sx={responsiveTableSx([])}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>From</TableCell>
                  <TableCell>To</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {linkedEdges.map((e) => (
                  <TableRow key={e.key}>
                    <TableCell>{labelFor(e.fromUid)}</TableCell>
                    <TableCell>{labelFor(e.toUid)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Stack>

      {fedOpen && (
        <FedAddContactDialog
          open
          links={fedLinks}
          members={people.filter((p) => byUid(p.uid)?.familyId === familyId)}
          onClose={() => setFedOpen(false)}
          onAdded={() => void loadContacts()}
        />
      )}

      <Dialog fullScreen={fullScreen} open={addOpen} onClose={() => setAddOpen(false)} fullWidth maxWidth="xs">
        <DialogTitle>Add contact</DialogTitle>
        <DialogContent>
          {dialogError && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {dialogError}
            </Alert>
          )}
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Phone number"
              value={addForm.phone}
              onChange={(e) => setAddForm({ ...addForm, phone: e.target.value })}
              placeholder="+15551234567"
              fullWidth
            />
            <TextField
              label="Name"
              value={addForm.name}
              onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
              helperText={NAME_HELP}
              fullWidth
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAddOpen(false)}>Cancel</Button>
          <Button onClick={() => void addContact()} disabled={!addValid}>
            Add
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog fullScreen={fullScreen} open={!!renaming} onClose={() => setRenaming(null)} fullWidth maxWidth="xs">
        <DialogTitle>Rename contact</DialogTitle>
        <DialogContent>
          {dialogError && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {dialogError}
            </Alert>
          )}
          <TextField
            label="Name"
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            helperText={NAME_HELP}
            fullWidth
            sx={{ mt: 1 }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRenaming(null)}>Cancel</Button>
          <Button onClick={() => void saveRename()} disabled={!renameValue.trim()}>
            Save
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog fullScreen={fullScreen} open={!!deleting} onClose={() => setDeleting(null)} fullWidth maxWidth="xs">
        <DialogTitle>Delete contact</DialogTitle>
        <DialogContent>
          <Typography>
            Delete {deleting?.displayName}? It is removed from every pager&apos;s SMS list.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleting(null)}>Cancel</Button>
          <Button color="error" onClick={() => void confirmDelete()}>
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

export default function FamilyContactsPage() {
  return <FamilyContactsInner />;
}
