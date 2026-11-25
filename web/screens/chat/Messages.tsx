"use client";

/** `/chat`: the Messages dashboard. Three independent sections, each with its own live reads,
 * skeleton and error: Conversations (`ConversationsSection`), Where they are (`WhereSection`: a card
 * per pager the viewer can see, linking to `/location`) and, for admins, All family conversations
 * (`AuditSection`, read-only). Phone: one column in that order. md and up: conversations left,
 * locations right, the audit list full width below. */

import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

import { useAuth } from "@/lib/auth-context";

import AuditSection from "./AuditSection";
import ConversationsSection from "./ConversationsSection";
import WhereSection, { useShowWhere } from "./WhereSection";

export default function Messages() {
  const { isFamilyAdmin } = useAuth();
  const showWhere = useShowWhere();

  return (
    <>
      <Typography variant="h5" component="h1" sx={{ mb: 2 }}>
        Messages
      </Typography>
      <Box
        sx={{
          display: "grid",
          gap: { xs: 3, md: 4 },
          gridTemplateColumns: { xs: "minmax(0, 1fr)", md: showWhere ? "minmax(0, 3fr) minmax(0, 2fr)" : "minmax(0, 1fr)" },
          alignItems: "start",
        }}
      >
        <ConversationsSection />
        {showWhere && <WhereSection />}
        {isFamilyAdmin && (
          <Box sx={{ gridColumn: { md: "1 / -1" }, minWidth: 0 }}>
            <AuditSection />
          </Box>
        )}
      </Box>
    </>
  );
}
