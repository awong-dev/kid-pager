"use client";

/** One titled section of a page: an `h2`, a one-line plain-language explanation, an optional
 * action (the section's own "Add" button) and the content. `id` makes it a `#fragment` target;
 * `scrollMarginTop` keeps the heading clear of the app bar and tab row when scrolled to. */

import type { ReactNode } from "react";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

export default function SectionBlock({
  id,
  title,
  description,
  action,
  children,
}: {
  id: string;
  title: string;
  description: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Stack component="section" id={id} aria-labelledby={`${id}-title`} spacing={2} sx={{ scrollMarginTop: { xs: 72, md: 128 } }}>
      <Stack spacing={0.5}>
        <Stack
          direction="row"
          spacing={1}
          useFlexGap
          sx={{ flexWrap: "wrap", justifyContent: "space-between", alignItems: "center" }}
        >
          <Typography id={`${id}-title`} variant="h6" component="h2">
            {title}
          </Typography>
          {action && (
            <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
              {action}
            </Stack>
          )}
        </Stack>
        <Typography variant="body2" color="text.secondary">
          {description}
        </Typography>
      </Stack>
      {children}
    </Stack>
  );
}
