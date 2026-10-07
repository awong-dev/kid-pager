/** Device-direct SMS types and validation: `/api/devices/{id}/sms-contacts` and `sms-log`. */

export interface SmsContact {
  name: string;
  phone: string;
}

export interface SmsContactsResponse {
  contacts: SmsContact[];
  pending: boolean;
}

export type SmsDirection = "out" | "in";
export type SmsStatus = "sent" | "failed" | "recv" | "blocked";

export interface SmsLogEntry {
  id: string;
  ts: number;
  smsTs?: number | null;
  // Malformed audit uploads (relay flags them): best-effort fields may be
  // null, `body` is null and `reason`/`rawHex` explain what arrived.
  dir: SmsDirection | null;
  peer: string | null;
  name?: string | null;
  st: SmsStatus | null;
  body: string | null;
  malformed?: boolean;
  reason?: string | null;
  rawHex?: string | null;
}

export interface SmsLogResponse {
  entries: SmsLogEntry[];
}

// A `GET /api/devices` row; `status` mirrors `DeviceStatusDoc`.
export interface DeviceListItem {
  id: string;
  label: string;
  status?: {
    state?: string | null;
    tls?: "unpinned" | "pinned" | "broken" | null;
    caFp?: string | null;
    smsLost?: number | null;
  } | null;
}

export const SMS_CONTACT_MAX = 8;
export const SMS_NAME_MAX = 16;
// The relay also caps a name at 24 UTF-8 bytes (the signed `cfg.sms` envelope's byte budget).
export const SMS_NAME_MAX_UTF8_BYTES = 24;
export const PHONE_E164_RE = /^\+[1-9]\d{6,14}$/;
export const PHONE_E164_EXAMPLE = "+12065550100";

const textEncoder = new TextEncoder();

export function isValidName(name: string): boolean {
  const trimmed = name.trim();
  if (trimmed.length < 1 || trimmed.length > SMS_NAME_MAX) return false;
  return textEncoder.encode(trimmed).length <= SMS_NAME_MAX_UTF8_BYTES;
}

export function isValidPhone(phone: string): boolean {
  return PHONE_E164_RE.test(phone.trim());
}

/** Mirrors the relay's PUT validation; returns one message per problem, empty = valid. */
export function validateContacts(contacts: SmsContact[]): string[] {
  const errors: string[] = [];
  if (contacts.length > SMS_CONTACT_MAX) {
    errors.push(`No more than ${SMS_CONTACT_MAX} contacts are allowed.`);
  }
  const seenPhones = new Set<string>();
  contacts.forEach((c, i) => {
    const row = i + 1;
    if (!isValidName(c.name)) {
      errors.push(`Row ${row}: name must be 1-${SMS_NAME_MAX} characters.`);
    }
    if (!isValidPhone(c.phone)) {
      errors.push(`Row ${row}: phone must be E.164, e.g. ${PHONE_E164_EXAMPLE}.`);
    } else {
      const normalized = c.phone.trim();
      if (seenPhones.has(normalized)) {
        errors.push(`Row ${row}: duplicate phone number.`);
      }
      seenPhones.add(normalized);
    }
  });
  return errors;
}

/** Direction arrow for the SMS log table. */
export function dirArrow(dir: SmsDirection | null): string {
  if (dir === null) return "—";
  return dir === "out" ? "→" : "←";
}

export function dirLabel(dir: SmsDirection): string {
  return dir === "out" ? "sent to pager's contact" : "received by pager";
}

export type StatusColor = "success" | "error" | "warning" | "default";

export function statusColor(st: SmsStatus): StatusColor {
  if (st === "sent" || st === "recv") return "success";
  if (st === "failed") return "error";
  return "warning"; // blocked
}
