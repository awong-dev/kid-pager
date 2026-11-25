import type { SxProps, Theme } from "@mui/material/styles";

/** Heights (px) of the app shell's chrome. `components/AppShell.tsx` sets these explicitly, and
 * `threadHeightSx` subtracts them, so a thread fills the viewport exactly (no double scrollbar). */
export const CHROME = {
  /** App bar: the slim phone bar (xs) and the desktop bar (md and up). */
  appBar: { xs: 56, md: 64 },
  /** The section tab row under the app bar (threads show none). */
  tabRow: 48,
  /** The content Container's vertical padding, top and bottom each. */
  padY: { xs: 16, md: 24 },
  /** The phone bottom navigation (hidden on threads). */
  bottomNav: 56,
} as const;

/** Full-height column for chat thread pages: viewport minus the app bar and the page padding.
 * Threads hide the bottom bar and the tab row; `tabRow: true` is for a page that shows one.
 * The phone bottom padding also covers the iOS home-indicator inset. `dvh` keeps iOS Safari's
 * collapsing toolbars from pushing the composer off screen. */
export function threadHeight(opts: { tabRow?: boolean } = {}): SxProps<Theme> {
  const tab = opts.tabRow ? CHROME.tabRow : 0;
  const xs = CHROME.appBar.xs + tab + 2 * CHROME.padY.xs;
  const md = CHROME.appBar.md + tab + 2 * CHROME.padY.md;
  const inset = "env(safe-area-inset-bottom)";
  return {
    height: { xs: `calc(100vh - ${xs}px - ${inset})`, md: `calc(100vh - ${md}px)` },
    "@supports (height: 100dvh)": {
      height: { xs: `calc(100dvh - ${xs}px - ${inset})`, md: `calc(100dvh - ${md}px)` },
    },
  };
}

export const threadHeightSx: SxProps<Theme> = threadHeight();
