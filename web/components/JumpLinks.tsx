"use client";

/** A row of chips that scroll to `#id` sections of the current page (and keep the fragment in the
 * address bar, so the view can be shared). No navigation: the screen stays mounted. */

import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";

export interface Jump {
  id: string;
  label: string;
}

export default function JumpLinks({ items }: { items: readonly Jump[] }) {
  return (
    <Stack component="nav" aria-label="On this page" direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
      {items.map((j) => (
        <Chip
          key={j.id}
          component="a"
          href={`#${j.id}`}
          clickable
          variant="outlined"
          label={j.label}
          onClick={(e: React.MouseEvent) => {
            e.preventDefault();
            document.getElementById(j.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
            window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${j.id}`);
          }}
        />
      ))}
    </Stack>
  );
}
