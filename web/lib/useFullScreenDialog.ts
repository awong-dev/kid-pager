"use client";

import { useMediaQuery, useTheme } from "@mui/material";

/** True below the `sm` breakpoint (phones), where dialogs go full screen. */
export function useFullScreenDialog(): boolean {
  const theme = useTheme();
  return useMediaQuery(theme.breakpoints.down("sm"));
}
