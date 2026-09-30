"use client";


import { useRouter } from "next/navigation";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import TextField from "@mui/material/TextField";

import { useAuth } from "@/lib/auth-context";

interface ChatOption {
  /** Route alias: E.164 digits for a number, the person/group alias otherwise. */
  alias: string;
  label: string;
}

/** Parses `+`, digits, spaces, dashes and parens as a phone number; a bare 10-digit number is
 * +1. Returns E.164 digits without `+`, or `null` if not phone-shaped. */
export function parsePhoneDigits(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed || !/^[+\d][\d\s().-]*$/.test(trimmed)) return null;
  const hasPlus = trimmed.startsWith("+");
  const digits = trimmed.replace(/\D/g, "");
  if (hasPlus) return digits.length >= 7 ? digits : null;
  if (digits.length === 10) return `1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return digits;
  return null;
}

/** "+1 555 123 4567" for a US/Canada number, "+<digits>" otherwise. */
export function formatPhoneDigits(digits: string): string {
  if (digits.length === 11 && digits.startsWith("1")) {
    const rest = digits.slice(1);
    return `+1 ${rest.slice(0, 3)} ${rest.slice(3, 6)} ${rest.slice(6)}`;
  }
  return `+${digits}`;
}

interface NewChatDialogProps {
  open: boolean;
  onClose: () => void;
}

export default function NewChatDialog({ open, onClose }: NewChatDialogProps) {
  const router = useRouter();
  const [inputValue, setInputValue] = useState("");

  const options = useMemo<ChatOption[]>(() => {
    const rows: ChatOption[] = [];
      rows.push({
      });
    }
    return rows;


  function reset() {
    setInputValue("");
  }

  function handleClose() {
    reset();
    onClose();
  }

  function go(alias: string) {
    reset();
    onClose();
    router.push(`/chat/${encodeURIComponent(alias)}`);
  }

    if (!trimmed) return;
  }

  return (
      <DialogTitle>New chat</DialogTitle>
      <DialogContent>
          autoHighlight
          groupBy={(o) => o.group}
          getOptionLabel={(o) => (typeof o === "string" ? o : o.label)}
          inputValue={inputValue}
          }}
          renderInput={(params) => (
          )}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Cancel</Button>
          Open
        </Button>
      </DialogActions>
    </Dialog>
  );
}
