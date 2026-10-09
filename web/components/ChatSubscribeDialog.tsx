"use client";

/** Subscribe dialog for a seen Google Chat / Voice conversation (`/family/chat` and the
 * `chat_unknown` alert card). If the member's people rule is `none` it sends `canReply: false`. */

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import ChatRosterEditor, { rosterErrors } from "@/components/ChatRosterEditor";
import { ApiError } from "@/lib/api";
import { NICK_MAX_CODEPOINTS, nickError, useBook } from "@/lib/book";
import { defaultRoster, outboundBlocksPeople, subscribeChat } from "@/lib/bridges";
import type { RosterEntry, SubscribeResult } from "@/lib/types";

export interface SubscribeTarget {
  bridgeId: string;
  ref: string;
  title: string;
  isGroup: boolean;
  /** Speakers seen so far (the roster rows). */
  people: string[];
}

function Body({
  ownerUid,
  target,
  onClose,
  onSubscribed,
}: {
  ownerUid: string;
  target: SubscribeTarget;
  onClose: () => void;
  onSubscribed: (result: SubscribeResult, pagerName: string) => void;
}) {
  const [name, setName] = useState([...target.title].slice(0, NICK_MAX_CODEPOINTS).join(""));
  const [canReply, setCanReply] = useState(true);
  const [roster, setRoster] = useState<RosterEntry[]>(() => defaultRoster(target.people));
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);


  const { data: book } = useBook(ownerUid);
  const pastCap = book !== null && book.entries.length >= book.pagerCap;

  const replyBlocked = policyOut !== null && outboundBlocksPeople(policyOut);
  const nameInvalid = name.trim() === "" ? "Required" : nickError(name);
  const rosterBad = target.isGroup && rosterErrors(roster).some((e) => e !== null);

  async function submit() {
    setBusy(true);
    setFailure(null);
    try {
      const res = await subscribeChat(target.bridgeId, target.ref, {
        pagerName: name.trim(),
        canReply: replyBlocked ? false : canReply,
        roster: target.isGroup ? roster : [],
      });
    } catch (e) {
      setFailure(e instanceof ApiError ? String(e.detail ?? e.message) : "Could not subscribe");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <DialogTitle>Subscribe to {target.title || "this conversation"}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {failure && <Alert severity="error">{failure}</Alert>}
          <TextField
            autoFocus
            fullWidth
            label="Name on pager"
            value={name}
            onChange={(e) => setName(e.target.value)}
            error={nameInvalid !== null}
            helperText={nameInvalid ?? `${[...name].length}/${NICK_MAX_CODEPOINTS}`}
          />
          {pastCap && (
            <Alert severity="info">
              The pager holds {book?.pagerCap} entries. This one will be reachable only from the web.
            </Alert>
          )}
          <div>
            <FormControlLabel
              control={
                <Switch
                  checked={replyBlocked ? false : canReply}
                  disabled={replyBlocked}
                  onChange={(e) => setCanReply(e.target.checked)}
                />
              }
              label="Kid can reply"
            />
            {replyBlocked && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                @{alias}&apos;s policy does not allow outbound messages
              </Typography>
            )}
          </div>
          {target.isGroup && <ChatRosterEditor roster={roster} onChange={setRoster} />}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={busy || nameInvalid !== null || rosterBad} onClick={() => void submit()}>
          Subscribe
        </Button>
      </DialogActions>
    </>
  );
}

export default function ChatSubscribeDialog({
  ownerUid,
  target,
  onClose,
  onSubscribed,
}: {
  ownerUid: string;
  /** `null` closes the dialog. */
  target: SubscribeTarget | null;
  onClose: () => void;
  onSubscribed: (result: SubscribeResult, pagerName: string) => void;
}) {
  // keyed per conversation so form state resets
  const key = useMemo(() => (target ? `${target.bridgeId}/${target.ref}` : "none"), [target]);
  return (
      {target && <Body key={key} ownerUid={ownerUid} target={target} onClose={onClose} onSubscribed={onSubscribed} />}
    </Dialog>
  );
}
