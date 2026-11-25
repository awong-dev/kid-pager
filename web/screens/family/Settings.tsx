"use client";

/** `/family/settings` "Family settings": the family itself (`FamilySection`) and how long message
 * history is kept (`RetentionSection`). */

import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import FamilySection from "./FamilySection";
import RetentionSection from "./RetentionSection";

export default function FamilySettingsPage() {
  return (
    <Stack spacing={4} sx={{ maxWidth: 560 }}>
      <Typography variant="h5" component="h1">
        Family settings
      </Typography>
      <FamilySection />
      <RetentionSection />
    </Stack>
  );
}
