"use client";

/** `/chat/[alias]`: one conversation thread (messages, composer, delivery chips, location link).
 * The alias comes from `usePathname()` because the route is a static shell. */

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import LocationOnIcon from "@mui/icons-material/LocationOn";
import SendIcon from "@mui/icons-material/Send";

import AppShell from "@/components/AppShell";
import RequireAuth from "@/components/RequireAuth";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";

const PAGE_SIZE_STEP = 50;

function utf8ByteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

function useAliasFromPath(): string | null {
  const pathname = usePathname();
  return useMemo(() => {
    if (!pathname) return null;
    const parts = pathname.split("/").filter(Boolean);
    if (parts.length < 2 || parts[0] !== "chat") return null;
    return decodeURIComponent(parts[1]!);
  }, [pathname]);
}

function ThreadInner({ alias }: { alias: string }) {
  const peerUid = aliasToUid(alias);

  const [pageSize, setPageSize] = useState(PAGE_SIZE_STEP);

  const [composer, setComposer] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [snack, setSnack] = useState<string | null>(null);

  const markedReadRef = useRef<Set<string>>(new Set());


  // `pageSize` grows on "load older" and the listener re-subscribes with the bigger limit.



  // Marks unread webapp deliveries read, only while at the bottom and the tab is visible.
    if (!me) return;
    for (const msg of messages) {
      if (msg.recipientUid !== me.uid) continue;
      if (markedReadRef.current.has(msg.id)) continue;
      const webapp = Object.values(msg.deliveries).find((d) => d.kind === "webapp");
      if (!webapp || webapp.state === "read") continue;
      markedReadRef.current.add(msg.id);
      void api.post(`/conversations/${encodeURIComponent(alias)}/messages/${msg.id}/read`).catch(() => {
        markedReadRef.current.delete(msg.id);
      });
    }

  const bodyBytes = utf8ByteLength(composer);
  const bodyValid =
    composer.trim().length > 0 &&
    bodyBytes <= BODY_MAX_UTF8_BYTES;

  async function handleSend() {
    if (!bodyValid || sending) return;
    setSending(true);
    setSendError(null);
    try {
      const resp = await api.post<{ id: string }>(
        `/conversations/${encodeURIComponent(alias)}/messages`,
        { body: composer }
      );
      setComposer("");
        }
      }
    } catch (err) {
    } finally {
      setSending(false);
    }
  }

  return (
        {device && (
          <Typography variant="caption" color="text.secondary">
            pager: {device.status.state ?? "unknown"}
          </Typography>
        )}
        <Box sx={{ flexGrow: 1 }} />
        )}
      </Stack>


      <Stack direction="row" spacing={1} sx={{ alignItems: "flex-end" }}>
        <TextField
          fullWidth
          multiline
          maxRows={4}
          placeholder="Message"
          value={composer}
          onChange={(e) => setComposer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void handleSend();
            }
          }}
        />
        <Button
          variant="contained"
          endIcon={sending ? <CircularProgress size={16} color="inherit" /> : <SendIcon />}
          disabled={!bodyValid || sending}
          onClick={() => void handleSend()}
        >
          Send
        </Button>
      </Stack>
      {sendError && <Alert severity="error">{sendError}</Alert>}

      <Snackbar
        open={snack !== null}
        autoHideDuration={5000}
        onClose={() => setSnack(null)}
        message={snack}
      />
    </Stack>
  );
}

export default function ThreadPageClient() {
  const alias = useAliasFromPath();
  return (
    <RequireAuth>
      <AppShell>
        {/* key remounts ThreadInner so local state resets per alias */}
        {alias ? <ThreadInner key={alias} alias={alias} /> : <CircularProgress />}
      </AppShell>
    </RequireAuth>
  );
}
