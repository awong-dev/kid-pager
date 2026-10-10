import type { SxProps, Theme } from "@mui/material/styles";

/** Full-height column for chat thread pages; `dvh` keeps iOS Safari's collapsing toolbars from
 * pushing the composer off screen. */
export const threadHeightSx: SxProps<Theme> = {
  height: { xs: "calc(100vh - 96px)", md: "calc(100vh - 140px)" },
  "@supports (height: 100dvh)": {
    height: { xs: "calc(100dvh - 96px)", md: "calc(100dvh - 140px)" },
  },
};
