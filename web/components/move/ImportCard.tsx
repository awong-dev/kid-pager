"use client";

/** Target: import the bundle (a dry run first, then the real import). */

import { useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { ApiError } from "@/lib/api";
import { type ImportReport, importBundle, passphraseProblem, reportOfRefusal } from "@/lib/move";

import { Section } from "./StatusParts";

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return typeof e.detail === "string" ? e.detail : e.message;
  return fallback;
}

function Report({ report }: { report: ImportReport }) {
  const counts = Object.entries(report.counts ?? {});
  return (
    <Stack spacing={1}>
      {(report.errors ?? []).map((m) => (
        <Alert key={m} severity="error">
          {m}
        </Alert>
      ))}
      {(report.emailsTaken ?? []).length > 0 && (
        <Alert severity="error">These emails belong to another account here: {report.emailsTaken!.join(", ")}.</Alert>
      )}
      {(report.emailsMissing ?? []).length > 0 && (
        <Alert severity="error">No answer for: {report.emailsMissing!.join(", ")}.</Alert>
      )}
      {(report.emailsInvalid ?? []).length > 0 && (
        <Alert severity="error">Not a usable email: {report.emailsInvalid!.join(", ")}.</Alert>
      )}
      {(report.collisions ?? []).length > 0 && (
        <Alert severity="error">
          Already on this server, so the bundle cannot be imported:{" "}
          {report.collisions!.map((c) => `${c.kind} ${c.id}`).join(", ")}.
        </Alert>
      )}
      {(report.notes ?? []).map((m) => (
        <Alert key={m} severity="info">
          {m}
        </Alert>
      ))}
      {(report.envSecrets ?? []).length > 0 && (
        <Alert severity="warning">
          The old server had these set in its environment. Copy them to this server by hand: {report.envSecrets!.join(", ")}.
        </Alert>
      )}
      {counts.length > 0 && (
        <Typography variant="body2" color="text.secondary">
          {counts.map(([k, v]) => `${v} ${k}`).join(", ")}
        </Typography>
      )}
    </Stack>
  );
}

export default function ImportCard({ onImported }: { onImported: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [pass, setPass] = useState("");
  const [busy, setBusy] = useState<"dry" | "real" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<{ dry: boolean; r: ImportReport } | null>(null);
  const problem = passphraseProblem(pass);
  const ready = !!file && !problem && busy === null;
  const dryOk = report?.dry === true && report.r.ok !== false && (report.r.errors ?? []).length === 0;

  async function run(dry: boolean) {
    if (!file) return;
    setBusy(dry ? "dry" : "real");
    setError(null);
    try {
      const r = await importBundle(file, pass, dry);
      setReport({ dry, r });
      if (!dry) onImported();
    } catch (e) {
      const refused = e instanceof ApiError && e.status === 409 ? reportOfRefusal(e.body) : null;
      setReport(refused ? { dry, r: { ...refused, ok: false } } : null);
      setError(errText(e, "The import failed."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Section
      title="Import the bundle"
      hint="From the old server's Move page. The dry run checks everything and changes nothing. Importing needs this server to be empty."
    >
      <Stack spacing={1.5}>
        <Typography variant="body2">
          On a brand-new home server this is the way in: create the first administrator on the first-run page, sign in,
          then import here. The import replaces that first administrator and its empty family with the imported
          deployment, so afterwards sign in as the people from the old server (with the claim link or the password set
          there).
        </Typography>
        <Box>
          <Button component="label" variant="outlined">
            {file ? "Choose another file" : "Choose the .kpdep file"}
            <input
              type="file"
              hidden
              accept=".kpdep"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setReport(null);
              }}
            />
          </Button>
          {file && (
            <Typography variant="body2" sx={{ mt: 0.5, overflowWrap: "anywhere" }}>
              {file.name}
            </Typography>
          )}
        </Box>
        <TextField
          label="Passphrase"
          type="password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          error={pass.length > 0 && !!problem}
          helperText={pass.length > 0 && problem ? problem : undefined}
          autoComplete="off"
          fullWidth
        />
        {error && <Alert severity="error">{error}</Alert>}
        {report && <Report report={report.r} />}
        {report?.dry && dryOk && <Alert severity="success">The dry run found no problems.</Alert>}
        <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
          <Button variant="outlined" disabled={!ready} onClick={() => void run(true)}>
            {busy === "dry" ? "Checking..." : "Dry run"}
          </Button>
          <Button variant="contained" disabled={!ready || !dryOk} onClick={() => void run(false)}>
            {busy === "real" ? "Importing..." : "Import"}
          </Button>
        </Stack>
      </Stack>
    </Section>
  );
}
