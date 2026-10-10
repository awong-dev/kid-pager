import type { SxProps, Theme } from "@mui/material/styles";

/** `sx` for a `TableContainer`: horizontal scroll; the given 1-based columns hide below `md`. */
export function responsiveTableSx(hideOnXs: number[] = [], stickyFirst = false): SxProps<Theme> {
  const hidden: Record<string, unknown> = {};
  for (const n of hideOnXs) {
    hidden[`& tr > :nth-child(${n})`] = { display: { xs: "none", md: "table-cell" } };
  }
  if (stickyFirst) {
    hidden["& tr > :first-child"] = { position: "sticky", left: 0, zIndex: 1, bgcolor: "background.paper" };
  }
  return { overflowX: "auto", maxWidth: "100%", ...hidden } as SxProps<Theme>;
}
