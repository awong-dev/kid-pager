"use client";

import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { Link } from "@/lib/router";

/** Unknown path. Rendered inside the signed-in shell (unknown paths are guarded like any other). */
export default function NotFound() {
  return (
    <Stack spacing={2} sx={{ alignItems: "flex-start", py: 4 }}>
      <Typography variant="h5">Page not found</Typography>
      <Typography color="text.secondary">There is nothing at this address.</Typography>
      <Button component={Link} href="/chat" variant="contained">
        Back to chat
      </Button>
    </Stack>
  );
}
