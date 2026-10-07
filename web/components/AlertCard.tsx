"use client";

/** One alert row; actions call `POST /api/family/alerts/{id}/{approve|block|dismiss}`. */

import { useState } from "react";
import Link from "next/link";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardActions from "@mui/material/CardActions";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutlined";
import PersonAddIcon from "@mui/icons-material/PersonAdd";
import SmsIcon from "@mui/icons-material/Sms";

import { formatPhoneDigits } from "@/components/NewChatDialog";
import { ApiError, api } from "@/lib/api";
import { familyQuery } from "@/lib/family-context";
import type { AlertDoc } from "@/lib/types";

export interface AlertRow extends AlertDoc {
  id: string;
}

function formatAge(ts: AlertDoc["ts"]): string {
  if (!ts) return "just now";
  const ms = Date.now() - ts.toDate().getTime();
  if (ms < 60_000) return "just now";
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function kindIcon(kind: AlertDoc["kind"]) {
  switch (kind) {
    case "sms_unknown":
      return <SmsIcon fontSize="small" />;
    case "new_conversation":
      return <ChatBubbleOutlineIcon fontSize="small" />;
    case "contact_request":
      return <PersonAddIcon fontSize="small" />;
  }
}

  switch (kind) {
    case "sms_unknown":
      return "Unknown number";
    case "new_conversation":
      return "New conversation";
    case "contact_request":
      return "Contact request";
  }
}

export default function AlertCard({ alert }: { alert: AlertRow }) {
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // sms_unknown approve dialog.
  const [smsOpen, setSmsOpen] = useState(false);
  const [smsName, setSmsName] = useState("");

  // 409 name collision on a contact_request approve: ask for another name.
  const [nameOpen, setNameOpen] = useState(false);
  const [nameValue, setNameValue] = useState("");
  const [nameConflict, setNameConflict] = useState<string | null>(null);

  const handled = alert.status !== "open";

  async function post(action: "approve" | "block" | "dismiss", body: unknown) {
    setSubmitting(true);
    setError(null);
    try {
      setSmsOpen(false);
      setNameOpen(false);
      setNameConflict(null);
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.detail ?? err.message) : `Failed to ${action}`;
      if (err instanceof ApiError && err.status === 409 && action === "approve") {
          setNameConflict(detail);
          setNameOpen(true);
        } else if (alert.kind === "sms_unknown") {
          setNameConflict(detail);
        } else {
          setError(detail);
        }
      } else {
        setError(detail);
      }
    } finally {
      setSubmitting(false);
    }
  }

  function openSmsApprove() {
    setError(null);
    setSmsName("");
    setNameConflict(null);
    setSmsOpen(true);
  }

  const isSms = alert.peerPhone != null;

  return (
    <Card variant="outlined">
      <CardContent>
          <Typography variant="caption" color="text.secondary">
            {formatAge(alert.ts)}
          </Typography>
          {handled && (
            <Chip
              size="small"
              variant="outlined"
              color={alert.status === "dismissed" ? "default" : "success"}
            />
          )}
        </Stack>

        {alert.kind === "sms_unknown" && (
          <>
            <Typography variant="body1">
            </Typography>
            {alert.preview && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                &quot;{alert.preview}&quot;
              </Typography>
            )}
          </>
        )}

        {alert.kind === "new_conversation" && (
          <>
            <Typography variant="body1">
            </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              </Typography>
            )}
          </>
        )}

        {alert.kind === "contact_request" && (
          <>
            {isSms ? (
              <>
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                  Approve: @{alert.subjectAlias}&apos;s pager can text this number.
                </Typography>
              </>
            ) : (
              <>
                <Typography variant="body1">
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                  Approve: @{alert.subjectAlias} can message @{alert.peerAlias}.
                </Typography>
              </>
            )}
          </>
        )}

        {error && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {error}
          </Alert>
        )}
      </CardContent>

      {!handled && (
        <CardActions>
          {alert.kind === "sms_unknown" && (
            <Button size="small" variant="contained" onClick={openSmsApprove} disabled={submitting}>
              Approve
            </Button>
          )}
          {alert.kind === "new_conversation" && (
            <>
              {alert.convKey && (
                <Button
                  size="small"
                  component={Link}
                  href={`/chat/view/${encodeURIComponent(alert.convKey)}`}
                >
                  View
                </Button>
              )}
              <Button
                size="small"
                variant="contained"
                onClick={() => void post("approve", {})}
                disabled={submitting}
              >
                Approve
              </Button>
            </>
          )}
            <Button size="small" variant="contained" onClick={() => void post("approve", {})} disabled={submitting}>
              Approve
            </Button>
          )}
            <Button size="small" color="warning" onClick={() => void post("block", {})} disabled={submitting}>
              Block
            </Button>
          )}
          <Button size="small" onClick={() => void post("dismiss", {})} disabled={submitting}>
            Dismiss
          </Button>
        </CardActions>
      )}

        <DialogTitle>Approve {alert.peerPhone ?? "number"}</DialogTitle>
        <DialogContent>
          {nameConflict && smsOpen && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {nameConflict}
            </Alert>
          )}
          <TextField
            autoFocus
            label="Name"
            value={smsName}
            onChange={(e) => setSmsName(e.target.value)}
            fullWidth
            sx={{ mt: 1 }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSmsOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={submitting || smsName.trim().length === 0}
            onClick={() => void post("approve", { name: smsName.trim(), forAlias: alert.subjectAlias })}
          >
            Approve
          </Button>
        </DialogActions>
      </Dialog>

        <DialogTitle>Choose another name</DialogTitle>
        <DialogContent>
          {nameConflict && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {nameConflict}
            </Alert>
          )}
          <TextField
            autoFocus
            label="Name"
            value={nameValue}
            onChange={(e) => setNameValue(e.target.value)}
            fullWidth
            sx={{ mt: 1 }}
            helperText="Must be unique in the family; the pager shows the first 16 characters."
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setNameOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={submitting || nameValue.trim().length === 0}
            onClick={() => void post("approve", { name: nameValue.trim() })}
          >
            Approve
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
