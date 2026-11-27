"use client";

/** Source, before Freeze: the new server's address and what it reports, the accounts panel, Freeze. */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import { useLive } from "@/lib/live";
import { allUsersQuery } from "@/lib/live/queries";
import {
  type AccountDraft,
  type AccountPerson,
  type PreflightResult,
  caFingerprintOf,
  caPemOf,
  changedDetails,
  checkAccounts,
  confirmOf,
  freeze,
  freezeProblems,
  groupHex,
  isTlsFailure,
  normalizeTargetUrl,
  preflight,
  preflightWarnings,
} from "@/lib/move";
import type { UserDoc } from "@/lib/types";

import AccountsPanel from "./AccountsPanel";
import ConfirmDialog from "./ConfirmDialog";
import { DowntimeList, Section } from "./StatusParts";

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return typeof e.detail === "string" ? e.detail : e.message;
  return fallback;
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <Box sx={{ display: "flex", flexDirection: { xs: "column", sm: "row" }, gap: { sm: 2 } }}>
      <Typography variant="body2" color="text.secondary" sx={{ minWidth: 150 }}>
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{ fontWeight: 600, overflowWrap: "anywhere", ...(mono ? { fontFamily: "monospace" } : {}) }}
      >
        {value}
      </Typography>
    </Box>
  );
}

const SERVER_NEEDS_ANSWER = "The server needs an answer for this person.";

/** The target's own certificate authority, for a home server that does not use a public one. */
function OwnCaControl({
  on,
  pem,
  onOn,
  onPem,
}: {
  on: boolean;
  pem: string;
  onOn: (on: boolean) => void;
  onPem: (pem: string) => void;
}) {
  const [fileError, setFileError] = useState<string | null>(null);
  async function pick(file: File | undefined) {
    if (!file) return;
    setFileError(null);
    if (file.size > 64 * 1024) {
      setFileError("That file is too big to be a certificate.");
      return;
    }
    onPem(await file.text());
  }
  const bad = on && pem.trim() !== "" && caPemOf(pem) === null;
  return (
    <Stack spacing={1}>
      <FormControlLabel
        control={<Checkbox checked={on} onChange={(e) => onOn(e.target.checked)} />}
        label="This server uses its own certificate authority"
      />
      {on && (
        <Stack spacing={1}>
          <Typography variant="body2">
            A home server that made its own certificates is not trusted by this server unless you give it that
            certificate. On the home server it is the file <code>data/certs/ca/ca.crt</code>. Paste its contents
            below, or choose the file. Do not use the private key.
          </Typography>
          <TextField
            label="CA certificate (PEM)"
            placeholder={"-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"}
            value={pem}
            onChange={(e) => onPem(e.target.value)}
            error={bad}
            helperText={bad ? "That is not a PEM certificate. It starts with -----BEGIN CERTIFICATE-----." : undefined}
            multiline
            minRows={4}
            maxRows={10}
            fullWidth
            slotProps={{ htmlInput: { spellCheck: false, style: { fontFamily: "monospace", fontSize: 12 } } }}
          />
          <Box>
            <Button component="label" variant="outlined" size="small">
              Choose the file
              <input type="file" hidden accept=".crt,.pem,.cer" onChange={(e) => void pick(e.target.files?.[0])} />
            </Button>
          </Box>
          {fileError && <Alert severity="error">{fileError}</Alert>}
        </Stack>
      )}
    </Stack>
  );
}

export default function SourceMoveStart({ onFrozen }: { onFrozen: () => void }) {
  const [urlText, setUrlText] = useState("");
  const [ownCa, setOwnCa] = useState(false);
  const [caText, setCaText] = useState("");
  const [checked, setChecked] = useState<{ url: string; caPem?: string; info: PreflightResult } | null>(null);
  const [changed, setChanged] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, AccountDraft>>({});
  const [touched, setTouched] = useState(false);
  const [serverErrors, setServerErrors] = useState<Readonly<Record<string, string>>>({});
  const [freezeOpen, setFreezeOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [freezeError, setFreezeError] = useState<string | null>(null);

  const { docs: userDocs } = useLive<UserDoc>(allUsersQuery());
  const people = useMemo<AccountPerson[]>(
    () =>
      userDocs
        .filter((d) => d.data.kind === "person" && !d.data.disabled)
        .map((d) => ({ uid: d.id, alias: d.data.alias, displayName: d.data.displayName, email: d.data.email ?? null }))
        .sort((a, b) => a.alias.localeCompare(b.alias)),
    [userDocs],
  );

  const info = checked?.info ?? null;
  const check = useMemo(
    () => (info ? checkAccounts(info.authBackend, people, drafts) : { errors: {}, ok: false, answers: [] }),
    [info, people, drafts],
  );

  async function runPreflight(note: string | null = null) {
    setError(null);
    setChanged(note);
    const t = normalizeTargetUrl(urlText);
    if ("error" in t) {
      setError(t.error);
      return;
    }
    const caPem = ownCa ? caPemOf(caText) : null;
    if (ownCa && !caPem) {
      setError("Paste the new server's CA certificate, or untick the box.");
      return;
    }
    setChecking(true);
    try {
      const result = await preflight(t.url, caPem ?? undefined);
      setChecked({ url: t.url, caPem: caPem ?? undefined, info: result });
      setConfirmed(false);
      setServerErrors({});
    } catch (e) {
      setChecked(null);
      if (e instanceof ApiError && isTlsFailure(e.status, e.detail)) {
        setError(
          ownCa
            ? "This server could not verify the new server's certificate with the CA certificate you gave. Check it is the ca.crt of that server, not another file."
            : "This server could not verify the new server's certificate. If it runs on a home server with its own certificate authority, tick \"This server uses its own certificate authority\" and add its certificate.",
        );
      } else {
        setError(errText(e, "Could not reach the new server."));
      }
    } finally {
      setChecking(false);
    }
  }

  async function runFreeze() {
    if (!checked) return;
    setBusy(true);
    setFreezeError(null);
    try {
      await freeze({
        targetBaseUrl: checked.url,
        ...(checked.caPem ? { caPem: checked.caPem } : {}),
        confirm: confirmOf(checked.info),
        accounts: check.answers,
      });
      setFreezeOpen(false);
      onFrozen();
    } catch (e) {
      const now = e instanceof ApiError && e.status === 409 ? changedDetails(e.body) : null;
      if (now) {
        setFreezeOpen(false);
        void runPreflight(
          `The new server's details changed since you checked them. It now reports broker ${now.host} port ${now.port}` +
            `${now.caSha ? `, certificate fingerprint ${groupHex(now.caSha)}` : ", no certificate authority"}. ` +
            "They are shown again below; confirm them if they are right.",
        );
      } else if (e instanceof ApiError && e.status === 422) {
        const p = freezeProblems(e.body);
        const named: Record<string, string> = { ...p.invalid };
        for (const uid of p.missing) named[uid] ??= SERVER_NEEDS_ANSWER;
        const count = Object.keys(named).length;
        setServerErrors(named);
        setTouched(true);
        setFreezeOpen(false);
        setError(
          count || p.unknown.length
            ? [
                count ? `The server refused the answers for ${count} ${count === 1 ? "person" : "people"}. They are marked below.` : "",
                p.unknown.length ? `It does not know these people: ${p.unknown.join(", ")}. Reload the page and try again.` : "",
              ]
                .filter(Boolean)
                .join(" ")
            : errText(e, "The accounts are incomplete."),
        );
      } else {
        setFreezeError(errText(e, "Freeze failed."));
      }
    } finally {
      setBusy(false);
    }
  }

  const fp = info ? caFingerprintOf(info) : null;
  const ready = !!info && confirmed && check.ok && people.length > 0;
  const backendText = info?.authBackend === "home" ? "Home (this server's own sign-in)" : "Cloud (Firebase)";

  return (
    <Stack spacing={2}>
      <DowntimeList frozen={false} />

      <Section
        step={1}
        title="The new server"
        hint="Enter its address. This server asks it who it is, and shows you what it said so you can check it is the right one."
      >
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
          <TextField
            label="New server address"
            placeholder="https://home.example/kid-pager"
            value={urlText}
            onChange={(e) => {
              setUrlText(e.target.value);
              setChecked(null);
              setChanged(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && void runPreflight()}
            autoCapitalize="none"
            fullWidth
          />
          <Button variant="outlined" onClick={() => void runPreflight()} disabled={checking || !urlText.trim()}>
            {checking ? "Checking..." : "Check"}
          </Button>
        </Stack>
        <OwnCaControl
          on={ownCa}
          pem={caText}
          onOn={(v) => {
            setOwnCa(v);
            setChecked(null);
          }}
          onPem={(v) => {
            setCaText(v);
            setChecked(null);
          }}
        />
        {changed && <Alert severity="warning">{changed}</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
        {info && (
          <Stack spacing={1} role="group" aria-label="What the new server reports">
            <Fact label="Address" value={info.baseUrl} />
            <Fact label="Sign-in" value={backendText} />
            <Fact label="Pager broker" value={`${info.broker.host} port ${info.broker.port}`} />
            <Fact label="Certificate fingerprint" value={fp ? groupHex(fp) : "none reported"} mono />
            {info.beam && <Fact label="Beam" value={`${info.beam.host} port ${info.beam.port}`} />}
            <Fact label="Software" value={`${info.relayVersion} (schema ${info.schemaVersion})`} />
            {preflightWarnings(info).map((w) => (
              <Alert key={w} severity="warning">
                {w}
              </Alert>
            ))}
            <FormControlLabel
              control={<Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />}
              label="The broker, port and fingerprint above are the new server's. Pagers will be told to trust exactly these."
            />
          </Stack>
        )}
      </Section>

      {info && (
        <Section step={2} title="People" hint="What each person needs to sign in on the new server.">
          <AccountsPanel
            backend={info.authBackend}
            people={people}
            drafts={drafts}
            onChange={(uid, d) => {
              setTouched(true);
              setDrafts((prev) => ({ ...prev, [uid]: d }));
              setServerErrors((prev) => {
                if (!(uid in prev)) return prev;
                const rest = { ...prev };
                delete rest[uid];
                return rest;
              });
            }}
            check={check}
            showErrors={touched}
            serverErrors={serverErrors}
          />
        </Section>
      )}

      {info && (
        <Section step={3} title="Freeze">
          <Typography variant="body2">
            Freeze stops this server taking new messages, and writes the bundle for the new server. You download it
            next.
          </Typography>
          {!check.ok && touched && <Alert severity="warning">Finish the people above first.</Alert>}
          <Box>
            <Button variant="contained" color="error" disabled={!ready} onClick={() => { setTouched(true); setFreezeOpen(true); }}>
              Freeze and start the move
            </Button>
            {!confirmed && (
              <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
                Confirm the new server&apos;s details in step 1 to enable this.
              </Typography>
            )}
          </Box>
        </Section>
      )}

      <ConfirmDialog
        open={freezeOpen}
        title="Freeze this server?"
        confirmLabel="Freeze now"
        danger
        busy={busy}
        error={freezeError}
        onConfirm={() => void runFreeze()}
        onClose={() => setFreezeOpen(false)}
      >
        <DowntimeList frozen={false} />
        <Typography variant="body2" sx={{ mt: 1.5 }}>
          Until you cancel the move, people cannot send from here. Cancel is available until the move is finished.
        </Typography>
      </ConfirmDialog>
    </Stack>
  );
}
