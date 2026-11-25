"use client";

/** "Outside contacts" on `/family/people`: the family's SMS contacts (add / rename / delete via
 * `/api/family/contacts`) and everything about other families: the federation links
 * (`FedLinksSection`, "Add from another family") and the read-only `LinkedPeople` list. */

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import FedAddContactDialog from "@/components/FedAddContactDialog";
import FedLinksSection from "@/components/FedLinksSection";
import SectionBlock from "@/components/SectionBlock";
import { ApiError, api } from "@/lib/api";
import type { BookResponse } from "@/lib/book";
import { useDirectory } from "@/lib/directory";
import { fedRemoteHost, fedRemoteName, getFamilyFed, type FedLink, type FedRef } from "@/lib/federation";
import { familyQuery, useFamily } from "@/lib/family-context";
import type { FamilyDoc } from "@/lib/types";
import { responsiveTableSx } from "@/lib/tableSx";
import { AddContactDialog, DeleteContactDialog, RenameContactDialog } from "./ContactDialogs";
import LinkedPeople from "./LinkedPeople";
import type { FamilyMembers } from "./useFamilyMembers";

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

type Dialog = { kind: "add" } | { kind: "rename"; row: ContactRow } | { kind: "delete"; row: ContactRow };

export default function ContactsSection({
  members,
  onSettled,
}: Pick<FamilyMembers, "members"> & {
  /** Called once the first contacts request has finished, either way (the page's "ready" signal). */
  onSettled?: () => void;
}) {
  const { familyId } = useFamily();
  const { byUid } = useDirectory();
  // contact uid -> member uids whose address book marks it "added from the pager".
  const [addedBy, setAddedBy] = useState<Record<string, string[]>>({});
  const [fedLinks, setFedLinks] = useState<FedLink[]>([]);
  const [fedOn, setFedOn] = useState<boolean | undefined>(undefined);
  const [fedOpen, setFedOpen] = useState(false);
  const [contacts, setContacts] = useState<ContactRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const loadContacts = useCallback(async () => {
    try {
      // returns a bare list
      const resp = await api.get<ContactRow[]>(`/family/contacts${familyQuery()}`);
      setContacts(Array.isArray(resp) ? resp : []);
      setLoadError(null);
    } catch (err) {
      setContacts((prev) => prev ?? []);
      setLoadError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to load contacts");
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await loadContacts();
      onSettled?.();
    })();
    // `onSettled` is only the page's one-shot signal; a new identity must not refetch
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [familyId, loadContacts]);

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
    Promise.all(
      members.map((m) =>
        api
          .get<BookResponse>(`/book?uid=${encodeURIComponent(m.uid)}`)
          .then((b) => b.entries.filter((e) => e.added && e.uid).map((e) => [e.uid as string, m.uid] as const))
          .catch(() => [] as (readonly [string, string])[])
      )
    ).then((rows) => {
      if (cancelled) return;
      const out: Record<string, string[]> = {};
      for (const [peer, owner] of rows.flat()) (out[peer] ??= []).push(owner);
      setAddedBy(out);
    });
    return () => {
      cancelled = true;
    };
  }, [familyId, members, contacts]);

  const labelFor = (uid: string): string => {
    const entry = byUid(uid);
    return entry ? `@${entry.alias}` : uid;
  };
  const people = members.filter((m) => m.kind === "person" && !m.disabled);
  const closeDialog = () => setDialog(null);
  const done = () => void loadContacts();

  return (
    <SectionBlock
      id="contacts"
      title="Outside contacts"
      description="People outside your family that pagers can text, and other families you are linked with."
      action={
        <>
          {fedLinks.some((l) => l.state === "active") && (
            <Button variant="outlined" onClick={() => setFedOpen(true)}>
              Add from another family
            </Button>
          )}
          <Button variant="contained" onClick={() => setDialog({ kind: "add" })}>
            Add contact
          </Button>
        </>
      }
    >
      <Typography variant="body2" color="text.secondary">
        Contacts are texted by the pager&apos;s own SMS. Approve a contact for a member (open the person under Family
        members, then Approved) to put it on their pager. Members with an SMS number text contacts through the relay;
        others use the pager&apos;s own SIM.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}
      {loadError && <Alert severity="error">{loadError}</Alert>}
      {contacts === null && <Skeleton variant="rounded" height={96} />}

      {contacts !== null && (
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
              {contacts.length === 0 && !loadError && (
                <TableRow>
                  <TableCell colSpan={4}>
                    <Typography variant="body2" color="text.secondary">
                      No outside contacts yet. Use Add contact.
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
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
                    <Button size="small" onClick={() => setDialog({ kind: "rename", row: c })}>
                      Rename
                    </Button>
                    <Button size="small" color="error" onClick={() => setDialog({ kind: "delete", row: c })}>
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <FedLinksSection federation={fedOn} members={people} />
      <LinkedPeople />

      {fedOpen && (
        <FedAddContactDialog open links={fedLinks} members={people} onClose={() => setFedOpen(false)} onAdded={done} />
      )}
      {dialog?.kind === "add" && <AddContactDialog onClose={closeDialog} onDone={done} onError={setError} />}
      {dialog?.kind === "rename" && (
        <RenameContactDialog contact={dialog.row} onClose={closeDialog} onDone={done} onError={setError} />
      )}
      {dialog?.kind === "delete" && (
        <DeleteContactDialog contact={dialog.row} onClose={closeDialog} onDone={done} onError={setError} />
      )}
    </SectionBlock>
  );
}
