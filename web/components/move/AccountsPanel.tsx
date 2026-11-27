"use client";

/** The accounts panel (docs/MOVE_DESIGN.md §9): what each person needs to sign in on the new
 * server. Home target: a claim link by default, optionally a password the admin sets. Firebase
 * target: an email, or an explicit "No sign-in", for every person. */

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import {
  type AccountDraft,
  type AccountPerson,
  type AccountsCheck,
  type MoveBackend,
  PASSWORD_MAX,
  PASSWORD_MIN,
  emptyDraft,
} from "@/lib/move";

export default function AccountsPanel({
  backend,
  people,
  drafts,
  onChange,
  check,
  showErrors,
  serverErrors,
}: {
  backend: MoveBackend;
  people: AccountPerson[];
  drafts: Record<string, AccountDraft>;
  onChange: (uid: string, next: AccountDraft) => void;
  check: AccountsCheck;
  /** Show per-person problems (after the first attempt, or once typed in). */
  showErrors: boolean;
  /** What the relay's Freeze refusal said about a person, by uid, until they change it. */
  serverErrors: Readonly<Record<string, string>>;
}) {
  if (people.length === 0) {
    return <Alert severity="info">There are no people to set up.</Alert>;
  }
  return (
    <Stack spacing={1.5}>
      {backend === "home" ? (
        <Typography variant="body2">
          Each person gets a claim link, shown to them in a banner here once the server is frozen. They open it on the
          new server and choose a password or a passkey. You can also set a password for someone below; they must
          change it when they first sign in.
        </Typography>
      ) : (
        <Typography variant="body2">
          The new server signs people in by email. Give every person an email, or choose No sign-in for someone who only
          carries a pager. They sign in with an emailed link.
        </Typography>
      )}
      <Box>
        {people.map((p) => {
          const d = drafts[p.uid] ?? emptyDraft(p);
          const err = (showErrors ? check.errors[p.uid] : undefined) ?? serverErrors[p.uid];
          const flagged = !!err;
          return (
            <Box key={p.uid} sx={{ py: 1.25, borderTop: 1, borderColor: "divider" }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                {p.displayName}{" "}
                <Typography component="span" variant="body2" color="text.secondary">
                  @{p.alias}
                </Typography>
              </Typography>
              {backend === "firebase" ? (
                <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 0.75, alignItems: { sm: "center" } }}>
                  <TextField
                    size="small"
                    type="email"
                    label="Email"
                    value={d.email}
                    disabled={d.noSignIn}
                    onChange={(e) => onChange(p.uid, { ...d, email: e.target.value })}
                    error={!!flagged}
                    helperText={err}
                    sx={{ flexGrow: 1, minWidth: { sm: 260 } }}
                    autoComplete="off"
                  />
                  <FormControlLabel
                    control={
                      <Checkbox checked={d.noSignIn} onChange={(e) => onChange(p.uid, { ...d, noSignIn: e.target.checked })} />
                    }
                    label="No sign-in"
                  />
                </Stack>
              ) : (
                <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 0.5, alignItems: { sm: "center" } }}>
                  <FormControlLabel
                    control={
                      <Checkbox
                        checked={d.setPassword}
                        onChange={(e) => onChange(p.uid, { ...d, setPassword: e.target.checked, password: "" })}
                      />
                    }
                    label="Set a password for the new server"
                  />
                  {d.setPassword && (
                    <TextField
                      size="small"
                      type="password"
                      label="Password"
                      value={d.password}
                      onChange={(e) => onChange(p.uid, { ...d, password: e.target.value })}
                      error={!!flagged}
                      helperText={err ?? `${PASSWORD_MIN} to ${PASSWORD_MAX} characters; they change it at first sign-in`}
                      autoComplete="new-password"
                    />
                  )}
                </Stack>
              )}
              {backend === "home" && !d.setPassword && err && (
                <Typography variant="body2" color="error" role="alert">
                  {err}
                </Typography>
              )}
            </Box>
          );
        })}
      </Box>
    </Stack>
  );
}
