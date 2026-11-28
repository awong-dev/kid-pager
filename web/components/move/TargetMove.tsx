"use client";

/** Target, after the import: the arrived / not-yet list with remedies, Beam, bridges, people,
 * federation links, the link to the old server's page, and End move. */

import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import SetupCodePanel, { type SetupCodeResult } from "@/screens/admin/SetupCodePanel";
import { ApiError, api } from "@/lib/api";
import {
  type BeamInfo,
  type MoveInfo,
  type PersonRow,
  type TargetDeviceRow,
  type TargetMove as TargetMoveData,
  TARGET_CHIP,
  beamActions,
  beamSwitch,
  beamTryTargets,
  beamSwitchBack,
  beamDeviceLine,
  beamSwitchBackBlock,
  beamWaitingWarning,
  bearerLabel,
  bearerTry,
  bearerTryResultText,
  waitingFromRefusal,
  bridgesSendBack,
  bridgeStillHere,
  bridgeTally,
  endMove,
  formatDuration,
  getOwnMoveInfo,
  hostOf,
  isDark,
  moveAdminUrl,
  peopleTally,
  remedyFor,
  sendBackState,
  sortTarget,
  targetTally,
  toMillis,
} from "@/lib/move";
import { formatRelativeAge } from "@/lib/time";
import { usePoll } from "@/lib/usePoll";

import ConfirmDialog from "./ConfirmDialog";
import { Section, StateChip, StatusRow, SummaryLine } from "./StatusParts";

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return typeof e.detail === "string" ? e.detail : e.message;
  return fallback;
}

/** Whether the old server still answers. A no-cors request has an opaque answer, which is enough:
 * it only rejects when nothing is listening. */
function useSourceUp(baseUrl: string): "up" | "down" | "unknown" {
  const poll = usePoll(
    async () => {
      try {
        await fetch(`${baseUrl.replace(/\/+$/, "")}/api/public/move-info`, {
          mode: "no-cors",
          cache: "no-store",
          signal: AbortSignal.timeout(6000),
        });
        return "up" as const;
      } catch {
        return "down" as const;
      }
    },
    30_000,
    baseUrl !== "",
  );
  return poll.data ?? "unknown";
}

function signedInFact(v: PersonRow["lastSignInAt"]): string | null {
  const ms = toMillis(v);
  return ms === null ? null : `signed in ${formatRelativeAge(ms)}`;
}

function lastSeen(v: TargetDeviceRow["lastSeenAt"]): string {
  const ms = toMillis(v);
  return ms === null ? "never seen" : `seen ${formatRelativeAge(ms)}`;
}

function PagerRow({
  row,
  now,
  host,
  sourceUp,
  onSetupCode,
}: {
  row: TargetDeviceRow;
  now: number;
  host: string;
  sourceUp: boolean;
  onSetupCode: (row: TargetDeviceRow) => void;
}) {
  const remedy = remedyFor(row, { host, sourceUp });
  const dark = toMillis(row.darkSince);
  return (
    <StatusRow
      title={row.label}
      subtitle={row.owner ? `@${row.owner}` : null}
      chips={
        <>
          <StateChip spec={TARGET_CHIP[row.state]} />
          {isDark(row) && dark !== null && (
            <StateChip spec={{ label: `Dark for ${formatDuration(now - dark)}`, color: "warning", filled: false, mark: "problem" }} />
          )}
        </>
      }
      facts={[bearerLabel(row.bearer), lastSeen(row.lastSeenAt)]}
      remedy={
        remedy && (
          <Stack spacing={1} sx={{ alignItems: "flex-start" }}>
            <Typography variant="body2">{remedy.text}</Typography>
            {remedy.setupCode && (
              <Button size="small" variant="outlined" onClick={() => onSetupCode(row)}>
                Issue a setup code
              </Button>
            )}
          </Stack>
        )
      }
    />
  );
}

function BeamCard({ beam, onChanged }: { beam: BeamInfo; onChanged: () => void }) {
  const [open, setOpen] = useState<"switch" | "back" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusedWaiting, setRefusedWaiting] = useState<string[]>([]);
  const [asking, setAsking] = useState(false);
  const [askResult, setAskResult] = useState<string | null>(null);
  const [askError, setAskError] = useState<string | null>(null);
  const a = beamActions(beam);
  const block = beamSwitchBackBlock(beam);
  const warning = beamWaitingWarning(beam) ?? (refusedWaiting.length > 0 ? beamWaitingWarning({ waitingCount: refusedWaiting.length }) : null);
  const force = a.switchNeedsForce || refusedWaiting.length > 0;
  const targets = beamTryTargets(beam.devices);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setOpen(null);
      setRefusedWaiting([]);
      onChanged();
    } catch (e) {
      const waiting = e instanceof ApiError && e.status === 409 ? waitingFromRefusal(e.body) : [];
      if (waiting.length > 0) {
        setRefusedWaiting(waiting);
      } else {
        setError(
          e instanceof ApiError && e.status === 409
            ? "A pager is on Beam here, so the group cannot be switched back."
            : errText(e, "That did not work."),
        );
      }
    } finally {
      setBusy(false);
    }
  }

  async function askPagers() {
    setAsking(true);
    setAskError(null);
    setAskResult(null);
    try {
      const r = await bearerTry(targets);
      const names = new Map(beam.devices.map((d) => [d.deviceId, d.label || d.deviceId]));
      setAskResult(bearerTryResultText(r, (id) => names.get(id) ?? id));
      onChanged();
    } catch (e) {
      setAskError(errText(e, "Could not ask the pagers."));
    } finally {
      setAsking(false);
    }
  }

  return (
    <Section
      title="Beam"
      hint="Pagers on Soracom Beam arrive over a direct connection, without anyone touching Soracom. Moving them back to Beam is an optional second step for afterwards."
    >
      <Typography variant="body2">
        {beam.count} Beam {beam.count === 1 ? "pager" : "pagers"}, {beam.movedCount} arrived, {beam.onBeamCount} on Beam here.{" "}
        {beam.switched ? "The Beam group is switched to this server." : "The Beam group still points at the old server."}
      </Typography>
      <Typography variant="subtitle2">Step 1: point the Beam group at this server</Typography>
      {!beam.soracom && !beam.switched && (
        <Alert severity="info">
          This server has no Soracom key. In the Soracom console, create a Beam group with destination{" "}
          <code>mqtts://{beam.host ?? "this server"}:{beam.port ?? 8884}</code> and pass-through credentials, move the
          SIMs into it, then press &quot;Done, I switched it&quot;.
        </Alert>
      )}
      {!beam.switched && warning && <Alert severity="warning">{warning}</Alert>}
      {error && <Alert severity="error">{error}</Alert>}
      {block && <Typography variant="body2" color="error.main">{block}</Typography>}
      {a.switchBackByHand && (
        <Typography variant="body2">The switch was made by hand, so switch the group back in the Soracom console.</Typography>
      )}
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
        <Button variant="contained" disabled={!a.canSwitch} onClick={() => { setError(null); setOpen("switch"); }}>
          {beam.soracom ? "Switch Beam group" : "Done, I switched it"}
        </Button>
        <Button variant="outlined" disabled={!a.canSwitchBack} onClick={() => { setError(null); setOpen("back"); }}>
          Switch back
        </Button>
      </Stack>

      <Typography variant="subtitle2">Step 2: switch the pagers to Beam</Typography>
      <Typography variant="body2">
        Each pager tries Beam and goes back to direct by itself if Beam does not reach this server.
      </Typography>
      {askError && <Alert severity="error">{askError}</Alert>}
      {askResult && <Alert severity="info">{askResult}</Alert>}
      <Box>
        <Button variant="contained" disabled={!a.canTryBeam || asking} onClick={() => void askPagers()}>
          Switch pagers to Beam
        </Button>
        {!beam.switched && (
          <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
            Available once the group is switched.
          </Typography>
        )}
      </Box>

      {beam.devices.length > 0 && (
        <Box>
          {beam.devices.map((d) => {
            const line = beamDeviceLine(d);
            return <StatusRow key={d.deviceId} title={line.name} chips={null} facts={[line.bearer]} detail={line.outcome} />;
          })}
        </Box>
      )}

      <ConfirmDialog
        open={open === "switch"}
        title={beam.soracom ? "Switch the Beam group?" : "Confirm the group is switched?"}
        confirmLabel={force ? (beam.soracom ? "Switch anyway" : "Done anyway") : beam.soracom ? "Switch" : "Done, I switched it"}
        danger={force}
        busy={busy}
        error={error}
        onConfirm={() => void run(() => beamSwitch(!beam.soracom, force))}
        onClose={() => { setOpen(null); setRefusedWaiting([]); }}
      >
        <Stack spacing={1}>
          {force && warning && <Alert severity="warning">{warning}</Alert>}
          <Typography variant="body2">
            {beam.soracom
              ? "Every SIM in the old group moves to a new group that points here. Pagers that are on this server stay; a Beam pager that has not arrived is cut off from the old server and may need a setup code."
              : "Only confirm once the SIMs are in the new group."}
          </Typography>
        </Stack>
      </ConfirmDialog>
      <ConfirmDialog
        open={open === "back"}
        title="Switch the Beam group back?"
        confirmLabel="Switch back"
        busy={busy}
        error={error}
        onConfirm={() => void run(beamSwitchBack)}
        onClose={() => setOpen(null)}
      >
        <Typography variant="body2">
          The SIMs return to the old group. Pagers that are not on Beam here keep using their direct connection.
        </Typography>
      </ConfirmDialog>
    </Section>
  );
}

export default function TargetMove({ move, onChanged }: { move: TargetMoveData; onChanged: () => void }) {
  const sourceStatus = useSourceUp(move.sourceBaseUrl);
  const [own, setOwn] = useState<MoveInfo | null>(null);
  const [setup, setSetup] = useState<{ result: SetupCodeResult; online: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"back" | "end" | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    void getOwnMoveInfo().then(setOwn);
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  const host = own?.broker.host ?? window.location.hostname;
  const rows = sortTarget(move.devices);
  const tally = targetTally(move.devices);
  const bridges = bridgeTally(move.bridges, now);
  const people = peopleTally(move.people);
  const sendBack = sendBackState(move.bridgeRedirect, move.bridges);
  const ended = move.state === "ended";
  const sourceUp = sourceStatus === "up";

  async function issueCode(row: TargetDeviceRow) {
    setError(null);
    try {
      const r = await api.post<{ setupCode: string; expiresAt: string; brokerPush: "pushed" | "manual"; brokerResult: "ok" | "manual" | "error"; manualAcl?: string[] | null }>(
        `/admin/devices/${encodeURIComponent(row.deviceId)}/rotate-credentials`,
      );
      if (r.brokerResult === "error") {
        setError(
          `The broker did not accept the new login for ${row.label}, so a setup code would not work. Make sure the broker is running and try again.`,
        );
        return;
      }
      setSetup({
        online: false,
        result: { deviceId: row.deviceId, label: row.label, setupCode: r.setupCode, expiresAt: r.expiresAt, brokerPush: r.brokerPush, manualAcl: r.manualAcl },
      });
      onChanged();
    } catch (e) {
      setError(errText(e, "Could not issue a setup code."));
    }
  }

  async function confirm(fn: () => Promise<unknown>) {
    setBusy(true);
    setDialogError(null);
    try {
      await fn();
      setDialog(null);
      onChanged();
    } catch (e) {
      setDialogError(errText(e, "That did not work."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack spacing={2}>
      {ended ? (
        <Alert severity="success">This move is finished. Logins stay as they are.</Alert>
      ) : (
        <Alert severity="info">
          <strong>This server is live.</strong> People can sign in and message here now. A page for a pager that has not
          arrived waits and is delivered when it does. Move {move.id}, imported from {hostOf(move.sourceBaseUrl)}.
        </Alert>
      )}

      <Section
        title="Pagers"
        hint="This is the authoritative list of which pagers have arrived. The old server's page shows whether each one has the order."
      >
        <SummaryLine tally={tally} />
        {error && <Alert severity="error">{error}</Alert>}
        <Box>
          {rows.map((r) => (
            <PagerRow key={r.deviceId} row={r} now={now} host={host} sourceUp={sourceUp} onSetupCode={(row) => void issueCode(row)} />
          ))}
        </Box>
      </Section>

      {move.beam && <BeamCard beam={move.beam} onChanged={onChanged} />}

      <Section title="Bridge phones" hint="Phones follow their redirect on their next request (within about 5 minutes) once it is set.">
        <Typography variant="subtitle2" role="status">
          {bridges.total === 0
            ? "No bridge phones."
            : `${bridges.total} bridge ${bridges.total === 1 ? "phone" : "phones"}: ${bridges.here} reaching this server, ${bridges.notYet} not arrived yet${bridges.gone ? `, ${bridges.gone} arrived and quiet` : ""}`}
        </Typography>
        <Box>
          {move.bridges.map((b) => {
            const ms = toMillis(b.lastSeenAt);
            const here = bridgeStillHere(b, now);
            return (
              <StatusRow
                key={b.bridgeId}
                title={b.label || b.bridgeId}
                subtitle={b.owner ? `@${b.owner}` : null}
                chips={
                  <StateChip
                    spec={
                      here
                        ? { label: "Reaching this server", color: "success", filled: true, mark: "done" }
                        : b.arrived
                          ? { label: "Quiet", color: "default", filled: false, mark: "wait" }
                          : { label: "Not arrived yet", color: "warning", filled: false, mark: "wait" }
                    }
                  />
                }
                facts={[ms === null ? "never seen here" : `seen ${formatRelativeAge(ms)}`]}
              />
            );
          })}
        </Box>
        {sendBack !== "none" && (
          <Box>
            <Button variant="outlined" disabled={sendBack === "sent"} onClick={() => { setDialogError(null); setDialog("back"); }}>
              {sendBack === "sent" ? "Bridges sent back" : "Send bridges back"}
            </Button>
            {sendBack === "sent" && (
              <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
                For a rollback. Wait until no bridge shows &quot;Reaching this server&quot;, then discard this server.
              </Typography>
            )}
          </Box>
        )}
      </Section>

      <Section title="People">
        <Typography variant="subtitle2" role="status">
          {people.signedIn} signed in, {people.notYet} not yet{people.noSignIn ? `, ${people.noSignIn} with no sign-in` : ""}
        </Typography>
        <Box>
          {move.people.map((p) => (
            <StatusRow
              key={p.uid}
              title={p.displayName || p.alias}
              subtitle={`@${p.alias}`}
              facts={[!p.noSignIn && p.signedIn ? signedInFact(p.lastSignInAt) : null]}
              chips={
                <StateChip
                  spec={
                    p.noSignIn
                      ? { label: "No sign-in", color: "default", filled: false, mark: "stop" }
                      : p.signedIn
                        ? { label: "Signed in", color: "success", filled: true, mark: "done" }
                        : { label: "Not signed in yet", color: "warning", filled: false, mark: "wait" }
                  }
                />
              }
            />
          ))}
        </Box>
      </Section>

      {move.fedLinks.length > 0 && (
        <Section title="Linked families" hint="Each other server is told this one is the new address, and retries until it accepts.">
          <Box>
            {move.fedLinks.map((l) => (
              <StatusRow
                key={l.linkId}
                title={l.peer}
                subtitle={l.family}
                chips={
                  <StateChip
                    spec={
                      l.announced
                        ? { label: "Accepted the new address", color: "success", filled: true, mark: "done" }
                        : { label: "Not announced yet", color: "warning", filled: false, mark: "wait" }
                    }
                  />
                }
              />
            ))}
          </Box>
        </Section>
      )}

      <Section title="The old server">
        {sourceStatus === "down" ? (
          <Typography variant="body2">The old server is not answering. Use the remedies shown beside each pager.</Typography>
        ) : (
          <Typography variant="body2">
            While it answers, its status page shows which pagers have the order.
          </Typography>
        )}
        {move.sourceBaseUrl && (
          <Link href={moveAdminUrl(move.sourceBaseUrl)} target="_blank" rel="noopener noreferrer" sx={{ overflowWrap: "anywhere" }}>
            {moveAdminUrl(move.sourceBaseUrl)}
          </Link>
        )}
      </Section>

      {!ended && (
        <Box>
          <Button variant="contained" onClick={() => { setDialogError(null); setDialog("end"); }}>
            Finish the move
          </Button>
          {tally.arrived < tally.total && (
            <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
              {tally.total - tally.arrived} pager{tally.total - tally.arrived === 1 ? " has" : "s have"} not arrived. Finishing keeps the record and logins; pagers can still be set up with a code.
            </Typography>
          )}
        </Box>
      )}

      <ConfirmDialog
        open={dialog === "back"}
        title="Send the bridge phones back?"
        confirmLabel="Send bridges back"
        busy={busy}
        error={dialogError}
        onConfirm={() => void confirm(bridgesSendBack)}
        onClose={() => setDialog(null)}
      >
        <Typography variant="body2">
          Use this to roll the move back. The phones are told to return to the old server on their next request. A phone
          that misses it returns by itself when this server stops answering.
        </Typography>
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "end"}
        title="Finish the move?"
        confirmLabel="Finish"
        busy={busy}
        error={dialogError}
        onConfirm={() => void confirm(endMove)}
        onClose={() => setDialog(null)}
      >
        <Typography variant="body2">
          The move record is archived. Pagers, bridges and logins stay. You can still issue setup codes.
          {tally.arrived < tally.total ? ` ${tally.total - tally.arrived} pager(s) have not arrived yet.` : ""}
        </Typography>
      </ConfirmDialog>

      {setup && <SetupCodePanel result={setup.result} online={setup.online} onClose={() => setSetup(null)} />}
    </Stack>
  );
}

