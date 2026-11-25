"use client";

/** Family export, backup and import dialogs for `/admin/families`. */

import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
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

import { ApiError, api } from "@/lib/api";
import { AUTH_BACKEND } from "@/lib/auth";
import { buildTemplate, looksLikeEmail, parseEmailList } from "@/lib/emailList";
import { isNotFrozen, isPassphraseRequired, listAdminFedLinks, reportFederationLines, type ReportFederation } from "@/lib/federation";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

export interface MoveReport {
  ok: boolean;
  sourceFamilyId: string;
  targetFamilyId: string;
  idSeed: string;
  history: boolean;
  counts: Record<string, number>;
  dropped: { collection: string; reason: string; count: number }[];
  collisions: { kind: string; id: string }[];
  errors: string[];
  notes: string[];
  kind: "backup" | "move";
  federation: ReportFederation;
  /** Cloud import only; absent from an older relay. */
  people?: PersonRow[];
  emailsMissing?: string[];
  emailsTaken?: string[];
  emailsInvalid?: string[];
}

/** One person in the file, with the relay's resolved email answer. */
export interface PersonRow {
  alias: string;
  targetAlias: string;
  name: string;
  role: string;
  sourceEmail: string | null;
  hadSignIn: boolean;
  email: string | null;
  /** `null` = unanswered. */
  signIn: boolean | null;
}

/** The admin's answer for one person: a typed email or "no sign-in". */
interface Answer {
  email: string;
  noSignIn: boolean;
}

interface ImportResult {
  report: MoveReport;
  invites: { uid: string; alias: string; code: string; expiresAt: string }[];
  accountsCreated: string[];
  accountsPending: string[];
  promoted: string | null;
  devices?: ImportedThing[];
  bridges?: ImportedThing[];
}

/** `familyexport.Imported`: a pager or bridge phone that arrived. */
interface ImportedThing {
  id: string;
  label: string;
  ownerAlias: string | null;
}

function errText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return typeof err.detail === "string" ? err.detail : err.message;
  return fallback;
}

function isReport(v: unknown): v is MoveReport {
  return !!v && typeof v === "object" && Array.isArray((v as MoveReport).collisions) && Array.isArray((v as MoveReport).errors);
}

// HTTP header values must be Latin-1 for `fetch`; keep to printable ASCII.
const PASS_RE = /^[\x20-\x7e]+$/;

function CopyButton({ text, label }: { text: string; label: string }) {
  return (
    <IconButton size="small" aria-label={label} onClick={() => void navigator.clipboard?.writeText(text)}>
      <ContentCopyIcon fontSize="small" />
    </IconButton>
  );
}

export function ExportDialog({
  family,
  onClose,
}: {
  family: { id: string; name: string; frozen: boolean } | null;
  onClose: () => void;
}) {
  const fullScreen = useFullScreenDialog();
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [history, setHistory] = useState(true);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Does the family have federation links? Then the file carries its key.
  const [hasLinks, setHasLinks] = useState(false);
  const familyId = family?.id ?? null;
  useEffect(() => {
    if (!familyId) return;
    let live = true;
    listAdminFedLinks()
      .then((ls) => live && setHasLinks(ls.some((l) => l.familyId === familyId)))
      .catch(() => live && setHasLinks(false)); // 404 (federation off) or any failure: no hint
    return () => {
      live = false;
    };
  }, [familyId]);

  function close() {
    setPass("");
    setPass2("");
    setError(null);
    onClose();
  }

  async function run() {
    if (!family) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await api.getBlob(
        `/admin/families/${encodeURIComponent(family.id)}/${live ? "backup" : "export"}?history=${history ? 1 : 0}`,
        { "X-Export-Passphrase": pass }
      );
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${family.id}-${new Date().toISOString().slice(0, 10)}.kpfam`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      close();
    } catch (err) {
      setError(
        isPassphraseRequired(err)
          ? "The relay says a passphrase is required: this file carries the family's federation key and must be encrypted."
          : isNotFrozen(err)
            ? "This family is not frozen. Freeze it first, or take a live backup."
            : errText(err, "Export failed")
      );
    } finally {
      setBusy(false);
    }
  }

  const mismatch = pass2.length > 0 && pass !== pass2;
  const badChars = pass.length > 0 && !PASS_RE.test(pass);
  const valid = pass.length >= 8 && pass === pass2 && !badChars && (family?.frozen || live);

  return (
    <Dialog fullScreen={fullScreen} open={family !== null} onClose={close} fullWidth maxWidth="xs">
      <DialogTitle>Export {family?.name}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {family && !family.frozen && (
            <Alert severity="warning">
              This family is not frozen. Freeze it first to move it, or tick &quot;Live backup&quot; below to take a backup
              of a running family.
            </Alert>
          )}
          {hasLinks && (
            <Alert severity="info">
              This family is linked to other families. This file carries the family&apos;s federation key. It must be
              encrypted, so a passphrase is required.
            </Alert>
          )}
          <Typography variant="body2" color="text.secondary">
            The file holds device keys and message text. It is encrypted with this passphrase; there is no way to
            recover the file without it.
          </Typography>
          <TextField
            label="Passphrase"
            type="password"
            value={pass}
            onChange={(e) => setPass(e.target.value)}
            helperText={badChars ? "Use plain ASCII characters only" : "At least 8 characters"}
            error={badChars}
            autoComplete="new-password"
            fullWidth
          />
          <TextField
            label="Passphrase again"
            type="password"
            value={pass2}
            onChange={(e) => setPass2(e.target.value)}
            error={mismatch}
            helperText={mismatch ? "Does not match" : undefined}
            autoComplete="new-password"
            fullWidth
          />
          <FormControlLabel
            control={<Switch checked={history} onChange={(e) => setHistory(e.target.checked)} />}
            label="Include history (messages, locations, battery, SMS log)"
          />
          {family && !family.frozen && (
            <FormControlLabel
              control={<Switch checked={live} onChange={(e) => setLive(e.target.checked)} />}
              label="Live backup (not a move)"
            />
          )}
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={close}>Cancel</Button>
        <Button onClick={() => void run()} disabled={!valid || busy}>
          {busy ? "Exporting..." : "Export"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

/** Live backup (no freeze): `GET /admin/families/{id}/backup`; the file has no federation key. */
export function BackupDialog({
  family,
  onClose,
}: {
  family: { id: string; name: string } | null;
  onClose: () => void;
}) {
  const fullScreen = useFullScreenDialog();
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [history, setHistory] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    setPass("");
    setPass2("");
    setError(null);
    onClose();
  }

  async function run() {
    if (!family) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await api.getBlob(`/admin/families/${encodeURIComponent(family.id)}/backup?history=${history ? 1 : 0}`, {
        "X-Export-Passphrase": pass,
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${family.id}-backup-${new Date().toISOString().slice(0, 10)}.kpfam`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      close();
    } catch (err) {
      setError(errText(err, "Backup failed"));
    } finally {
      setBusy(false);
    }
  }

  const mismatch = pass2.length > 0 && pass !== pass2;
  const badChars = pass.length > 0 && !PASS_RE.test(pass);
  const valid = pass.length >= 8 && pass === pass2 && !badChars;

  return (
    <Dialog fullScreen={fullScreen} open={family !== null} onClose={close} fullWidth maxWidth="xs">
      <DialogTitle>Back up {family?.name}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Typography variant="body2">
            A backup holds this family&apos;s people, contacts, settings and message history. Keep it somewhere safe.
            Links to other families are saved but must be re-authorized after a restore.
          </Typography>
          <Typography variant="body2" color="text.secondary">
            The file holds device keys and message text. It is encrypted with this passphrase; there is no way to
            recover the file without it.
          </Typography>
          <TextField
            label="Passphrase"
            type="password"
            value={pass}
            onChange={(e) => setPass(e.target.value)}
            helperText={badChars ? "Use plain ASCII characters only" : "At least 8 characters"}
            error={badChars}
            autoComplete="new-password"
            fullWidth
          />
          <TextField
            label="Passphrase again"
            type="password"
            value={pass2}
            onChange={(e) => setPass2(e.target.value)}
            error={mismatch}
            helperText={mismatch ? "Does not match" : undefined}
            autoComplete="new-password"
            fullWidth
          />
          <FormControlLabel
            control={<Switch checked={history} onChange={(e) => setHistory(e.target.checked)} />}
            label="Include history (messages, locations, battery, SMS log)"
          />
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={close}>Cancel</Button>
        <Button onClick={() => void run()} disabled={!valid || busy}>
          {busy ? "Backing up..." : "Back up"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

interface AliasRow {
  from: string;
  to: string;
}

const isAdminRole = (role: string) => role === "admin" || role === "super";

/** Email step: table of everyone in the file plus a bulk paste list. */
function EmailStep({
  people,
  answers,
  setAnswer,
  applyAnswers,
  flags,
  stale,
}: {
  people: PersonRow[];
  answers: Record<string, Answer>;
  setAnswer: (alias: string, a: Answer) => void;
  applyAnswers: (m: Record<string, Answer>) => void;
  flags: Record<string, string>;
  stale: boolean;
}) {
  const [text, setText] = useState("");
  const [note, setNote] = useState<{ bad: string[]; unknown: string[]; applied: number } | null>(null);

  function apply() {
    const parsed = parseEmailList(text);
    const next: Record<string, Answer> = {};
    const unknown: string[] = [];
    for (const e of parsed.entries) {
      const p = people.find((x) => x.alias.toLowerCase() === e.alias || x.targetAlias.toLowerCase() === e.alias);
      if (!p) {
        unknown.push(`@${e.alias}`);
        continue;
      }
      next[p.alias] = e.email === null ? { email: "", noSignIn: true } : { email: e.email, noSignIn: false };
    }
    applyAnswers(next);
    setNote({
      bad: parsed.bad.map((b) => `line ${b.line}: ${b.text}`),
      unknown,
      applied: Object.keys(next).length,
    });
  }

  function template() {
    const t = buildTemplate(people.map((p) => ({ alias: p.alias, email: answers[p.alias]?.email ?? "" })));
    void navigator.clipboard?.writeText(t);
  }

  return (
    <Stack spacing={1.5}>
      <Typography variant="subtitle2">Sign-in emails</Typography>
      <Typography variant="body2" color="text.secondary">
        Every person needs an email or &quot;No sign-in&quot; (children without their own account). At least one admin
        needs an email.
      </Typography>
      <TextField
        label="Paste a list"
        placeholder={"alias, email\nkid, -"}
        helperText="One person per line: alias, email. Use - for no sign-in. Lines starting with # are ignored."
        value={text}
        onChange={(e) => setText(e.target.value)}
        multiline
        minRows={3}
        maxRows={8}
        fullWidth
        slotProps={{ htmlInput: { sx: { fontFamily: "monospace" } } }}
      />
      <Stack direction="row" spacing={1}>
        <Button size="small" variant="outlined" onClick={apply} disabled={!text.trim()}>
          Apply list
        </Button>
        <Button size="small" onClick={template}>
          Copy template
        </Button>
      </Stack>
      {note && (
        <Stack spacing={0.5}>
          {note.applied > 0 && <Typography variant="body2">Applied to {note.applied} people.</Typography>}
          {note.bad.length > 0 && (
            <Alert severity="warning">Could not read: {note.bad.join("; ")}</Alert>
          )}
          {note.unknown.length > 0 && (
            <Alert severity="warning">Not in the file: {note.unknown.join(", ")}</Alert>
          )}
        </Stack>
      )}
      <TableContainer sx={{ maxWidth: "100%", overflowX: "auto" }}>
        <Table size="small" sx={{ minWidth: 520 }}>
          <TableHead>
            <TableRow>
              <TableCell>Person</TableCell>
              <TableCell>Role</TableCell>
              <TableCell>Email</TableCell>
              <TableCell>No sign-in</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {people.map((p) => {
              const a = answers[p.alias] ?? { email: "", noSignIn: false };
              const err = flags[p.alias];
              return (
                <TableRow key={p.alias}>
                  <TableCell>
                    @{p.alias}
                    {p.targetAlias !== p.alias ? ` (as @${p.targetAlias})` : ""}
                    <Typography variant="caption" color="text.secondary" component="div">
                      {p.name}
                    </Typography>
                  </TableCell>
                  <TableCell>{p.role}</TableCell>
                  <TableCell sx={{ minWidth: 200 }}>
                    <TextField
                      size="small"
                      value={a.noSignIn ? "" : a.email}
                      disabled={a.noSignIn}
                      onChange={(e) => setAnswer(p.alias, { email: e.target.value, noSignIn: false })}
                      error={!!err}
                      helperText={err}
                      slotProps={{ htmlInput: { "aria-label": `email for ${p.alias}`, inputMode: "email" } }}
                      fullWidth
                    />
                  </TableCell>
                  <TableCell>
                    <Checkbox
                      checked={a.noSignIn}
                      onChange={(e) => setAnswer(p.alias, { email: a.email, noSignIn: e.target.checked })}
                      slotProps={{ input: { "aria-label": `no sign-in for ${p.alias}` } }}
                    />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>
      {stale && (
        <Typography variant="caption" color="text.secondary">
          Emails changed since the last check; press &quot;Check again&quot; before importing.
        </Typography>
      )}
    </Stack>
  );
}

/** Federation part of the import preview / result. */
function FederationNotes({ report, variant }: { report: MoveReport; variant: "preview" | "result" }) {
  const f = reportFederationLines(report.federation);
  const isBackup = report.kind === "backup";
  if (!f.reauth && !(f.announcing && !isBackup)) return null;
  const body = (
    <>
      {f.reauth && <div>{f.reauth}</div>}
      {f.announcing && !isBackup && <div>Linked families are told about the move automatically; their admins get a notice.</div>}
    </>
  );
  if (variant === "result") return <Alert severity="info">{body}</Alert>;
  return (
    <div>
      <Typography variant="subtitle2">Federation</Typography>
      <Typography variant="body2" component="div">
        {body}
      </Typography>
    </div>
  );
}

function ReportView({ report }: { report: MoveReport }) {
  const counts = Object.entries(report.counts);
  const needsSuper = report.errors.some((e) => /--super|no super/.test(e));
  return (
    <Stack spacing={1.5}>
      <Alert severity={report.ok ? "success" : "warning"}>
        {report.ok
          ? `Ready to ${report.kind === "backup" ? "restore" : "import"} ${report.sourceFamilyId} as ${report.targetFamilyId}.`
          : "This file cannot be imported yet. Fix the items below and run the check again."}
      </Alert>
      {report.errors.map((e) => (
        <Alert key={e} severity="error">
          {e}
        </Alert>
      ))}
      {report.collisions.length > 0 && (
        <div>
          <Typography variant="subtitle2">Collisions (nothing is merged)</Typography>
          <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap", mt: 0.5 }}>
            {report.collisions.map((c) => (
              <Chip key={`${c.kind}:${c.id}`} size="small" color="error" variant="outlined" label={`${c.kind}: ${c.id}`} />
            ))}
          </Stack>
        </div>
      )}
      <div>
        <Typography variant="subtitle2">What will be imported {report.history ? "" : "(no history in this file)"}</Typography>
        <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap", mt: 0.5 }}>
          {counts.map(([k, n]) => (
            <Chip key={k} size="small" label={`${k}: ${n}`} />
          ))}
        </Stack>
      </div>
      {report.dropped.length > 0 && (
        <div>
          <Typography variant="subtitle2">Left out</Typography>
          {report.dropped.map((d) => (
            <Typography key={`${d.collection}:${d.reason}`} variant="body2">
              {d.collection}: {d.count} ({d.reason})
            </Typography>
          ))}
        </div>
      )}
      {report.notes.map((n) => (
        <Typography key={n} variant="body2" color="text.secondary">
          {n}
        </Typography>
      ))}
      <FederationNotes report={report} variant="preview" />
      <div>
        <Typography variant="subtitle2">After the import you will need to</Typography>
        <Typography variant="body2">
          Give each of the {report.counts.devices ?? 0} pager(s) a new setup code; re-pair {report.counts.bridges ?? 0}{" "}
          bridge phone(s); hand out invite codes
          {needsSuper ? "; promote a super admin (see below)" : ""}.
        </Typography>
      </div>
    </Stack>
  );
}

function Checklist({ result }: { result: ImportResult }) {
  const r = result.report;
  const devices = r.counts.devices ?? 0;
  const bridges = r.counts.bridges ?? 0;
  return (
    <Stack spacing={1.5}>
      <Alert severity="success">
        {r.kind === "backup" ? (
          <>
            Restored as family <b>{r.targetFamilyId}</b>.
          </>
        ) : (
          <>
            Imported as family <b>{r.targetFamilyId}</b>. Check the source server still has the family frozen, and
            delete it there only after you have verified this copy.
          </>
        )}
      </Alert>
      <FederationNotes report={r} variant="result" />
      <Typography variant="subtitle2">Checklist</Typography>
      <Typography variant="body2">
        1. Pagers ({devices}): open{" "}
        <Link href="/admin/devices">System &gt; All devices</Link>, press &quot;New setup code&quot; on each and type it on
        the pager. Soracom Beam users also repoint the Beam group at this server.
      </Typography>
      {(result.devices ?? []).map((d) => (
        <Typography key={d.id} variant="body2" sx={{ pl: 2 }}>
          - New setup code for {d.label}
          {d.ownerAlias ? ` (@${d.ownerAlias})` : ""}
        </Typography>
      ))}
      <Typography variant="body2">
        2. Bridge phones ({bridges}): imported bridges arrive unpaired. Press &quot;New pair code&quot; on each
        bridge row (<Link href="/family/devices">Bridge phones</Link>) and enter this server in the phone app.
      </Typography>
      {(result.bridges ?? []).map((b) => (
        <Typography key={b.id} variant="body2" sx={{ pl: 2 }}>
          - Re-pair {b.label}
          {b.ownerAlias ? ` (@${b.ownerAlias})` : ""}
        </Typography>
      ))}
      <Typography variant="body2">3. Invite codes (shown once, copy them now):</Typography>
      {result.invites.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          None were issued.
        </Typography>
      )}
      {result.invites.map((i) => (
        <Stack key={i.uid} direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <Chip size="small" label={`@${i.alias}`} />
          <Typography sx={{ fontFamily: "monospace" }}>{i.code}</Typography>
          <CopyButton text={i.code} label={`copy invite code for ${i.alias}`} />
          <Typography variant="caption" color="text.secondary">
            expires {i.expiresAt}
          </Typography>
        </Stack>
      ))}
      {result.accountsCreated.length > 0 && (
        <Typography variant="body2">
          4. Accounts created for: {result.accountsCreated.map((a) => `@${a}`).join(", ")}.
        </Typography>
      )}
      {result.accountsPending.length > 0 && (
        <Typography variant="body2">
          {result.accountsCreated.length > 0 ? "" : "4. "}No sign-in: {result.accountsPending.map((a) => `@${a}`).join(", ")}{" "}
          &mdash; you can add an email later from the person&apos;s page (
          <Link href="/family/people">Family &gt; People &amp; contacts</Link>).
        </Typography>
      )}
      {result.promoted && (
        <Typography variant="body2" color="text.secondary">
          @{result.promoted} is now the super admin.
        </Typography>
      )}
    </Stack>
  );
}

export function ImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const fullScreen = useFullScreenDialog();
  const cloud = AUTH_BACKEND !== "home";
  const [file, setFile] = useState<File | null>(null);
  const [pass, setPass] = useState("");
  const [asId, setAsId] = useState("");
  const [aliases, setAliases] = useState<AliasRow[]>([]);
  const [superAlias, setSuperAlias] = useState("");
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  // People whose answer was edited since the last check: server flags for them are stale.
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [checkedQuery, setCheckedQuery] = useState<string | null>(null);
  const [report, setReport] = useState<MoveReport | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    setFile(null);
    setPass("");
    setAsId("");
    setAliases([]);
    setSuperAlias("");
    setAnswers({});
    setTouched(new Set());
    setCheckedQuery(null);
    setReport(null);
    setResult(null);
    setError(null);
    onClose();
  }

  const people = report?.people && report.people.length > 0 ? report.people : null;

  function query(dryRun: boolean): string {
    const q = new URLSearchParams();
    if (dryRun) q.set("dryRun", "1");
    if (asId.trim()) q.set("as", asId.trim());
    for (const a of aliases) if (a.from && a.to.trim()) q.append("alias", `${a.from}=${a.to.trim()}`);
    if (!cloud && superAlias.trim()) q.set("super", superAlias.trim());
    if (cloud && people) {
      for (const p of people) {
        const a = answers[p.alias];
        if (!a) continue;
        if (a.noSignIn) q.append("email", `${p.alias}=-`);
        else if (a.email.trim()) q.append("email", `${p.alias}=${a.email.trim()}`);
      }
    }
    return `/admin/families/import?${q.toString()}`;
  }

  // Alias collisions name the (possibly already renamed) target alias; add a
  // row for any not yet in the table so the admin can type the new name.
  function absorb(r: MoveReport, q: string) {
    setReport(r);
    setCheckedQuery(q);
    setTouched(new Set());
    if (r.people) {
      // Seed from the relay's resolved answer; keep what the admin already typed.
      setAnswers((cur) => {
        const next = { ...cur };
        for (const p of r.people ?? []) {
          if (next[p.alias]) continue;
          next[p.alias] =
            p.signIn === false
              ? { email: "", noSignIn: true }
              : { email: p.email ?? p.sourceEmail ?? "", noSignIn: false };
        }
        return next;
      });
    }
    setAliases((rows) => {
      const next = [...rows];
      for (const c of r.collisions) {
        if (c.kind === "alias" && !next.some((x) => x.from === c.id || x.to === c.id)) next.push({ from: c.id, to: "" });
      }
      return next;
    });
  }

  async function run(dryRun: boolean) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const q = query(dryRun);
      const out = await api.postFile<MoveReport | ImportResult>(q, file, { "X-Export-Passphrase": pass });
      if (dryRun) absorb(out as MoveReport, q);
      else setResult(out as ImportResult);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && isReport(err.detail)) absorb(err.detail, query(true));
      else setError(errText(err, "Import failed"));
    } finally {
      setBusy(false);
    }
  }

  // Client-side checks on the email table (the relay re-checks everything).
  const { flags, adminOk, emailChecks } = useMemo(() => {
    const f: Record<string, string> = {};
    if (!people) return { flags: f, adminOk: true, emailChecks: true };
    const seen = new Map<string, string>();
    let allAnswered = true;
    let adminHasEmail = false;
    for (const p of people) {
      const a = answers[p.alias] ?? { email: "", noSignIn: false };
      const em = a.email.trim();
      if (a.noSignIn) continue;
      if (!em) {
        allAnswered = false;
        if (!touched.has(p.alias) && report?.emailsMissing?.includes(p.alias)) f[p.alias] = "needed";
        continue;
      }
      if (!looksLikeEmail(em)) {
        f[p.alias] = "not an email address";
        continue;
      }
      const key = em.toLowerCase();
      const other = seen.get(key);
      if (other) f[p.alias] = f[other] = "same email as another person";
      else seen.set(key, p.alias);
      if (isAdminRole(p.role)) adminHasEmail = true;
      if (!touched.has(p.alias)) {
        if (report?.emailsInvalid?.includes(p.alias)) f[p.alias] ??= "not an email address";
        if (report?.emailsTaken?.includes(p.alias)) f[p.alias] ??= "already used by another account";
      }
    }
    const adminOk = adminHasEmail;
    return { flags: f, adminOk, emailChecks: allAnswered && adminOk && Object.keys(f).length === 0 };
  }, [people, answers, touched, report]);
  const stale = cloud && !!people && checkedQuery !== null && checkedQuery !== query(true);
  const canImport = !!report?.ok && report.errors.length === 0 && (!cloud || !people || (emailChecks && !stale));

  const needsSuper = !cloud && (report?.errors.some((e) => /--super|no super/.test(e)) ?? false);
  const badChars = pass.length > 0 && !PASS_RE.test(pass);
  const canCheck = !!file && pass.length > 0 && !badChars && !busy;

  return (
    <Dialog fullScreen={fullScreen} open={open} onClose={close} fullWidth maxWidth="sm">
      <DialogTitle>{(result?.report ?? report)?.kind === "backup" ? "Restore from backup" : "Import family"}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {result ? (
            <Checklist result={result} />
          ) : (
            <>
              <Button variant="outlined" component="label" disabled={busy}>
                {file ? file.name : "Choose .kpfam file"}
                <input
                  hidden
                  type="file"
                  accept=".kpfam,application/octet-stream"
                  onChange={(e) => {
                    setFile(e.target.files?.[0] ?? null);
                    setReport(null);
                    setAnswers({});
                    setTouched(new Set());
                    setCheckedQuery(null);
                  }}
                />
              </Button>
              <TextField
                label="Passphrase"
                type="password"
                value={pass}
                onChange={(e) => setPass(e.target.value)}
                error={badChars}
                helperText={badChars ? "Use plain ASCII characters only" : undefined}
                autoComplete="off"
                fullWidth
              />
              <TextField
                label="New family id (optional)"
                value={asId}
                onChange={(e) => setAsId(e.target.value)}
                helperText="Needed when the file's family id is already taken here (a home server's family is usually 'default')"
                fullWidth
              />
              {aliases.length > 0 && (
                <Stack spacing={1}>
                  <Typography variant="subtitle2">Rename aliases that collide</Typography>
                  {aliases.map((a, i) => (
                    <Stack key={a.from} direction="row" spacing={1} sx={{ alignItems: "center" }}>
                      <Typography sx={{ minWidth: 90 }}>@{a.from}</Typography>
                      <TextField
                        size="small"
                        label="New alias"
                        value={a.to}
                        onChange={(e) =>
                          setAliases(aliases.map((x, j) => (j === i ? { ...x, to: e.target.value.toLowerCase() } : x)))
                        }
                        fullWidth
                      />
                    </Stack>
                  ))}
                </Stack>
              )}
              {needsSuper && (
                <TextField
                  label="Super admin alias"
                  value={superAlias}
                  onChange={(e) => setSuperAlias(e.target.value)}
                  helperText="This server has no super admin: name one imported admin to promote"
                  fullWidth
                />
              )}
              {cloud && people && (
                <>
                  <EmailStep
                    people={people}
                    answers={answers}
                    setAnswer={(alias, a) => {
                      setAnswers((cur) => ({ ...cur, [alias]: a }));
                      setTouched((t) => new Set(t).add(alias));
                    }}
                    applyAnswers={(m) => {
                      setAnswers((cur) => ({ ...cur, ...m }));
                      setTouched((t) => new Set([...t, ...Object.keys(m)]));
                    }}
                    flags={flags}
                    stale={stale}
                  />
                  {!adminOk && <Alert severity="warning">At least one admin needs an email</Alert>}
                </>
              )}
              {report && <ReportView report={report} />}
            </>
          )}
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={close}>{result ? "Done" : "Cancel"}</Button>
        {!result && (
          <>
            <Button onClick={() => void run(true)} disabled={!canCheck}>
              {report ? "Check again" : "Check file"}
            </Button>
            <Button variant="contained" onClick={() => void run(false)} disabled={!canCheck || !canImport}>
              {report?.kind === "backup" ? "Confirm restore" : "Confirm import"}
            </Button>
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}
