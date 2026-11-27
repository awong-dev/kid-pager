"use client";

/** Source, after Freeze: bundle download, Start cutover, the per-pager status list, bridges,
 * people and Cancel move. */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import {
  type BridgeRow,
  type SourceDeviceRow,
  type SourceMove,
  SOURCE_CHIP,
  bearerLabel,
  cancelDevice,
  cancelMove,
  capableCount,
  cutover,
  downloadBundle,
  formatDuration,
  hostOf,
  moveAdminUrl,
  passphraseProblem,
  resendOrder,
  sortSource,
  sourceDetail,
  sourceStage,
  sourceTally,
  toMillis,
} from "@/lib/move";
import { formatRelativeAge } from "@/lib/time";

import ConfirmDialog from "./ConfirmDialog";
import { DowntimeList, Section, StateChip, StatusRow, SummaryLine } from "./StatusParts";

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return typeof e.detail === "string" ? e.detail : e.message;
  return fallback;
}

/** A 502 on resend is the broker refusing the order; retryable. */
function retryable(e: unknown): never {
  if (e instanceof ApiError && e.status === 502) {
    throw new ApiError(e.status, `${errText(e, "")} You can send the order again.`, e.body);
  }
  throw e;
}

function seen(v: SourceDeviceRow["lastSeenAt"]): string | null {
  const ms = toMillis(v);
  return ms === null ? "never seen" : `seen ${formatRelativeAge(ms)}`;
}

function PagerRow({ row, now, cutoverStarted, onAct }: { row: SourceDeviceRow; now: number; cutoverStarted: boolean; onAct: (fn: () => Promise<unknown>) => void }) {
  const stage = sourceStage(row);
  const dark = toMillis(row.darkSince);
  const live = stage !== "moved" && stage !== "cancelled";
  const ordered = !!row.sentAt;
  return (
    <StatusRow
      title={row.label}
      subtitle={row.owner ? `@${row.owner}` : null}
      chips={
        <>
          <StateChip spec={SOURCE_CHIP[stage]} />
          {dark !== null && live && <StateChip spec={{ label: `Dark ${formatDuration(now - dark)}`, color: "warning", filled: false, mark: "problem" }} />}
        </>
      }
      facts={[
        row.capable ? "can move by itself" : "cannot move by itself",
        ordered ? "order sent" : "no order yet",
        row.ackAt ? "acked" : ordered ? "not acked" : null,
        bearerLabel(row.bearer),
        seen(row.lastSeenAt),
      ]}
      detail={sourceDetail(row)}
      actions={
        live && (
          <>
            {cutoverStarted && row.capable && !ordered && (
              <Button size="small" variant="outlined" onClick={() => onAct(() => cutover([row.deviceId]))}>
                Send the order
              </Button>
            )}
            {ordered && (
              <Button size="small" variant="outlined" onClick={() => onAct(() => resendOrder(row.deviceId).catch(retryable))}>
                Send the order again
              </Button>
            )}
            {ordered && (
              <Button size="small" onClick={() => onAct(() => cancelDevice(row.deviceId))}>
                Cancel for this pager
              </Button>
            )}
          </>
        )
      }
    />
  );
}

function BridgeLine({ b }: { b: BridgeRow }) {
  const ms = toMillis(b.lastSeenAt);
  return (
    <StatusRow
      title={b.label || b.bridgeId}
      subtitle={b.owner ? `@${b.owner}` : null}
      chips={
        <StateChip
          spec={
            b.redirected
              ? { label: "Told to move", color: "info", filled: true, mark: "done" }
              : { label: "Not told yet", color: "default", filled: false, mark: "wait" }
          }
        />
      }
      facts={[ms === null ? "never seen" : `seen ${formatRelativeAge(ms)}`]}
    />
  );
}

export default function SourceMoveFrozen({ move, onChanged }: { move: SourceMove; onChanged: () => void }) {
  const targetUrl = move.target.baseUrl;
  const cutoverStarted = move.state === "cutover";
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [dl, setDl] = useState<{ busy: boolean; error: string | null; done: boolean }>({ busy: false, error: null, done: false });
  const [action, setAction] = useState<{ error: string | null }>({ error: null });
  const [cutoverOpen, setCutoverOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);
  const [dialogError, setDialogError] = useState<string | null>(null);

  // Absent while the new server does not answer.
  const imported = move.targetImported ?? cutoverStarted;

  const rows = sortSource(move.devices);
  const tally = sourceTally(move.devices);
  const capable = capableCount(move.devices);
  const passProblem = passphraseProblem(pass, pass2);

  async function download() {
    setDl({ busy: true, error: null, done: false });
    try {
      const blob = await downloadBundle(pass);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${hostOf(window.location.href)}-${new Date().toISOString().slice(0, 10)}.kpdep`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
      setDl({ busy: false, error: null, done: true });
    } catch (e) {
      setDl({ busy: false, error: errText(e, "Download failed."), done: false });
    }
  }

  async function act(fn: () => Promise<unknown>) {
    setAction({ error: null });
    try {
      await fn();
      onChanged();
    } catch (e) {
      setAction({ error: errText(e, "That did not work.") });
    }
  }

  async function confirmDialog(fn: () => Promise<unknown>, close: () => void) {
    setBusy(true);
    setDialogError(null);
    try {
      await fn();
      close();
      onChanged();
    } catch (e) {
      setDialogError(errText(e, "That did not work."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack spacing={2}>
      <Alert severity="warning">
        <strong>This server is frozen for the move to {hostOf(targetUrl)}.</strong> Move {move.id}.
        {cutoverStarted ? " Orders have gone to the pagers." : " The pagers have not been ordered yet."}
      </Alert>
      {move.target.privateCa && (
        <Typography variant="body2" color="text.secondary">
          The new server was checked with its own certificate authority.
        </Typography>
      )}
      <DowntimeList frozen />

      <Section
        step={1}
        title="Download the bundle"
        hint="Everything the new server needs, encrypted with a passphrase you choose. Delete the file after the import. You can download it again."
      >
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
          <TextField label="Passphrase" type="password" value={pass} onChange={(e) => setPass(e.target.value)} autoComplete="new-password" fullWidth />
          <TextField label="Passphrase again" type="password" value={pass2} onChange={(e) => setPass2(e.target.value)} autoComplete="new-password" fullWidth />
        </Stack>
        <Typography variant="caption" color={pass && passProblem ? "error" : "text.secondary"}>
          {pass ? (passProblem ?? "Looks good.") : "At least 8 characters. Without it the file cannot be read."}
        </Typography>
        {dl.error && <Alert severity="error">{dl.error}</Alert>}
        {dl.done && <Alert severity="success">Downloaded. Import it on the new server.</Alert>}
        <Box>
          <Button variant="contained" disabled={!!passProblem || dl.busy} onClick={() => void download()}>
            {dl.busy ? "Preparing..." : "Download bundle"}
          </Button>
        </Box>
      </Section>

      <Section
        step={2}
        title="Import on the new server"
        hint="Sign in there as the super admin, open System > Move and import the file."
      >
        <Link href={moveAdminUrl(targetUrl)} target="_blank" rel="noopener noreferrer" sx={{ overflowWrap: "anywhere" }}>
          {moveAdminUrl(targetUrl)}
        </Link>
        <Typography variant="body2" role="status">
          {imported ? "The new server reports the import. You can start the cutover." : "Waiting for the new server to report the import."}
        </Typography>
      </Section>

      <Section
        step={3}
        title="Start cutover"
        hint="Orders every move-capable pager to switch to the new server. Pagers that are off get the order when they next come online, as long as this server stays up. Beam pagers also need their Beam group switched on the new server."
      >
        <Box>
          <Button variant="contained" disabled={!imported || cutoverStarted || capable === 0} onClick={() => { setDialogError(null); setCutoverOpen(true); }}>
            {cutoverStarted ? "Cutover started" : `Start cutover (${capable} ${capable === 1 ? "pager" : "pagers"})`}
          </Button>
          {!imported && (
            <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
              Disabled until the new server reports the import.
            </Typography>
          )}
        </Box>
      </Section>

      <Section title="Pagers" hint="The new server's Move page is the authoritative list of which pagers have arrived. This one shows what each pager has told this server.">
        <SummaryLine tally={tally} />
        {action.error && <Alert severity="error">{action.error}</Alert>}
        <Box>
          {rows.map((r) => (
            <PagerRow key={r.deviceId} row={r} now={now} cutoverStarted={cutoverStarted} onAct={(fn) => void act(fn)} />
          ))}
        </Box>
      </Section>

      {move.bridges.length > 0 && (
        <Section title="Bridge phones" hint="Told to move in their next request after the import (within about 5 minutes).">
          <Box>
            {move.bridges.map((b) => (
              <BridgeLine key={b.bridgeId} b={b} />
            ))}
          </Box>
        </Section>
      )}

      {move.people.length > 0 && (
        <Section title="People" hint="How each person signs in on the new server. They see this in a banner when they open this server.">
          <Box>
            {move.people.map((p) => (
              <StatusRow
                key={p.uid}
                title={p.displayName || p.alias}
                subtitle={`@${p.alias}`}
                chips={
                  <StateChip
                    spec={
                      p.noSignIn
                        ? { label: "No sign-in", color: "default", filled: false, mark: "stop" }
                        : { label: p.email ? `Email ${p.email}` : "Claim link", color: "info", filled: false, mark: "wait" }
                    }
                  />
                }
              />
            ))}
          </Box>
        </Section>
      )}

      <Box>
        <Button color="error" variant="outlined" onClick={() => { setDialogError(null); setCancelOpen(true); }}>
          Cancel the move
        </Button>
      </Box>

      <ConfirmDialog
        open={cutoverOpen}
        title="Start cutover?"
        confirmLabel="Order the pagers"
        busy={busy}
        error={dialogError}
        onConfirm={() => void confirmDialog(() => cutover(), () => setCutoverOpen(false))}
        onClose={() => setCutoverOpen(false)}
      >
        <Typography variant="body2">
          {capable} {capable === 1 ? "pager" : "pagers"} will be ordered to move to {hostOf(targetUrl)}. Pagers that are
          not move-capable ({tally.total - capable}) will need a setup code from the new server.
        </Typography>
      </ConfirmDialog>

      <ConfirmDialog
        open={cancelOpen}
        title="Cancel the move?"
        confirmLabel="Cancel the move"
        danger
        busy={busy}
        error={dialogError}
        onConfirm={() => void confirmDialog(() => cancelMove(), () => setCancelOpen(false))}
        onClose={() => setCancelOpen(false)}
      >
        <Stack spacing={1}>
          <Typography variant="body2">This server is unfrozen and takes messages again.</Typography>
          {cutoverStarted ? (
            <Typography variant="body2">
              Pagers that have the order are told to cancel it. A pager that already moved stays on the new server; it
              comes back only by a new move the other way or a setup code from this server. If the Beam group was
              switched, switch it back on the new server (possible only while no Beam pager has moved), and send the
              bridges back there.
            </Typography>
          ) : (
            <Typography variant="body2">
              On the new server, press &quot;Send bridges back&quot; and wait until it shows no bridge still there, then
              discard the new server.
            </Typography>
          )}
          <Typography variant="body2">
            <strong>Anything written on the new server since the import is lost</strong> unless you run a full reverse
            move.
          </Typography>
        </Stack>
      </ConfirmDialog>
    </Stack>
  );
}
