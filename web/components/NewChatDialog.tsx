"use client";

/** "New chat" dialog: search select over the address book; only navigates to `/chat/{alias}`. */

import { useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Autocomplete, { createFilterOptions } from "@mui/material/Autocomplete";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import TextField from "@mui/material/TextField";
import PhoneIcon from "@mui/icons-material/Phone";

import { Link, useNavigate } from "@/lib/router";
import { useAuth } from "@/lib/auth-context";
import { useLive } from "@/lib/live";
import { userQuery } from "@/lib/live/queries";
import { BOOK_GROUP_ORDER, type BookGroup, bookGroup, useBook } from "@/lib/book";
import type { UserDoc } from "@/lib/types";
import { useFullScreenDialog } from "@/lib/useFullScreenDialog";

interface ChatOption {
  /** Route alias: E.164 digits for a number, the person/group alias otherwise. */
  alias: string;
  label: string;
  group: BookGroup;
  displayName: string;
  phone: string | null;
  /** Admins also see unsendable entries, greyed out. */
  disabled: boolean;
  external: boolean;
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

const filter = createFilterOptions<ChatOption>({
  ignoreCase: true,
  ignoreAccents: true,
  stringify: (o) => `${o.label} ${o.displayName} ${o.alias} ${o.phone ?? ""}`,
});

interface NewChatDialogProps {
  open: boolean;
  onClose: () => void;
}

export default function NewChatDialog({ open, onClose }: NewChatDialogProps) {
  const fullScreen = useFullScreenDialog();
  const navigate = useNavigate();
  const { isFamilyAdmin, me } = useAuth();
  // Live, so an admin setting the number takes effect without a reload.
  const meUid = me?.uid;
  const { docs: meDocs } = useLive<UserDoc>(meUid ? userQuery(meUid) : null);
  const hasSmsNumber = Boolean(meDocs[0]?.data.smsNumber);
  const { data } = useBook();
  const [inputValue, setInputValue] = useState("");
  const [value, setValue] = useState<ChatOption | string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const entries = useMemo(() => data?.entries ?? [], [data]);
  const options = useMemo<ChatOption[]>(() => {
    const rows: ChatOption[] = [];
    for (const e of entries) {
      // SMS contacts are chat targets only through the user's relay number.
      // A bridged Google Chat contact (`chat` set) needs no relay number.
      if (e.kind === "external" && !e.chat && (!hasSmsNumber || !e.sendable)) continue;
      if (!e.sendable && !isFamilyAdmin) continue;
      rows.push({
        alias: e.alias,
        label: e.kind === "external" && e.phone ? `${e.label} · ${formatPhoneDigits(e.phone.replace(/^\+/, ""))}` : e.label,
        group: bookGroup(e),
        displayName: e.displayName,
        phone: e.phone ?? null,
        disabled: !e.sendable,
        external: e.kind === "external",
      });
    }
    rows.sort(
      (a, b) =>
        BOOK_GROUP_ORDER.indexOf(a.group) - BOOK_GROUP_ORDER.indexOf(b.group) || a.label.localeCompare(b.label)
    );
    return rows;
  }, [entries, isFamilyAdmin, hasSmsNumber]);

  const bookEmpty = data !== null && !entries.some((e) => e.sendable);

  function reset() {
    setInputValue("");
    setValue(null);
    setNotice(null);
  }

  function handleClose() {
    reset();
    onClose();
  }

  function go(alias: string) {
    reset();
    onClose();
    navigate.push(`/chat/${encodeURIComponent(alias)}`);
  }

  // Admin free text: an alias typed with or without its leading `@`
  // navigates the same way; a phone-shaped value by its E.164 digits.
  function handleSubmit(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (parsePhoneDigits(trimmed) !== null) {
      setNotice("Numbers are texted from the pager. Add one under Family → Contacts.");
      return;
    }
    go(trimmed.replace(/^@/, ""));
  }

  function open_() {
    if (value && typeof value !== "string") go(value.alias);
    else handleSubmit(typeof value === "string" ? value : inputValue);
  }

  const canOpen = value !== null || (isFamilyAdmin && inputValue.trim() !== "");

  return (
    <Dialog fullScreen={fullScreen} open={open} onClose={handleClose} fullWidth maxWidth="xs">
      <DialogTitle>New chat</DialogTitle>
      <DialogContent>
        {bookEmpty && (
          <Alert severity="info" sx={{ mb: 1 }}>
            Your address book is empty. Ask a family admin to approve people for you.{" "}
            <Link href="/settings/book">Address book</Link>
          </Alert>
        )}
        <Autocomplete<ChatOption, false, false, boolean>
          freeSolo={isFamilyAdmin}
          autoHighlight
          openOnFocus
          options={options}
          value={value}
          groupBy={(o) => o.group}
          getOptionLabel={(o) => (typeof o === "string" ? o : o.label)}
          getOptionDisabled={(o) => typeof o !== "string" && o.disabled}
          isOptionEqualToValue={(a, b) =>
            typeof a !== "string" && typeof b !== "string" ? a.alias === b.alias : a === b
          }
          noOptionsText="No one in your address book matches"
          inputValue={inputValue}
          onInputChange={(_, v) => {
            setInputValue(v);
            setNotice(null);
          }}
          onChange={(_, v) => {
            setValue(v);
            if (!v) return;
            if (typeof v === "string") handleSubmit(v);
            else go(v.alias);
          }}
          filterOptions={filter}
          renderOption={(props, o) => {
            const { key, ...rest } = props;
            return (
              <li key={key} {...rest}>
                {o.external && o.phone && <PhoneIcon fontSize="small" sx={{ mr: 1 }} />}
                {o.label}
              </li>
            );
          }}
          renderInput={(params) => (
            <TextField
              {...params}
              autoFocus
              margin="dense"
              label={isFamilyAdmin ? "Name or @alias" : "Search your address book"}
            />
          )}
        />
        {notice && (
          <Alert severity="info" sx={{ mt: 1 }}>
            {notice}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Cancel</Button>
        <Button variant="contained" disabled={!canOpen} onClick={open_}>
          Open
        </Button>
      </DialogActions>
    </Dialog>
  );
}
