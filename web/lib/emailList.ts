/** Bulk email list parser: one `alias,email` / `alias=email` / `alias<space>email` per line;
 * `-` as the email means no sign-in; `#` lines and blank lines are ignored. */

export interface ListEntry {
  line: number;
  alias: string;
  /** `null` = "no sign-in" (`-`). */
  email: string | null;
}

export interface ParsedList {
  entries: ListEntry[];
  bad: { line: number; text: string }[];
}

const LINE_RE = /^@?([^\s,=@]+)(?:\s*[,=]\s*|\s+)(\S.*)?$/;
const ALIAS_ONLY_RE = /^@?([^\s,=@]+)\s*[,=]?\s*$/;

export function parseEmailList(text: string): ParsedList {
  const out: ParsedList = { entries: [], bad: [] };
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = i + 1;
    const s = raw.trim();
    if (!s || s.startsWith("#")) return;
    if (ALIAS_ONLY_RE.test(s)) return; // template line, nothing filled in yet
    const m = LINE_RE.exec(s);
    const value = m?.[2]?.trim();
    if (!m || !value || /\s/.test(value)) {
      out.bad.push({ line, text: s });
      return;
    }
    out.entries.push({ line, alias: m[1].toLowerCase(), email: value === "-" ? null : value });
  });
  return out;
}

const EMAIL_RE = /^[^\s@,=]+@[^\s@,=]+\.[^\s@,=]+$/;

export function looksLikeEmail(s: string): boolean {
  return EMAIL_RE.test(s.trim());
}

/** Template text for "Copy template": one `alias,email` line per person. */
export function buildTemplate(rows: { alias: string; email: string }[]): string {
  return rows.map((r) => `${r.alias},${r.email}`).join("\n") + "\n";
}
