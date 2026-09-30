"use client";

import { useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";

import { useAuth } from "@/lib/auth-context";

function LoadingScreen() {
  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", height: "60vh" }}>
      <CircularProgress />
    </Box>
  );
}

/** Client-side route guard (UX only; the relay and rules enforce access).
 * `requireRole: 'admin'` accepts a family admin or super; `'super'` accepts only super. */
export default function RequireAuth({
  children,
  requireAdmin = false,
  requireRole,
}: {
  children: ReactNode;
  /** @deprecated use `requireRole="admin"` instead. */
  requireAdmin?: boolean;
  requireRole?: "admin" | "super";
}) {
  const router = useRouter();
  const effectiveRole = requireRole ?? (requireAdmin ? "admin" : undefined);
  const allowed =
    effectiveRole === undefined ? true : effectiveRole === "super" ? isSuper : isFamilyAdmin;

  useEffect(() => {
    if (status === "signed-out" || status === "not-registered") {
    } else if (status === "signed-in" && !allowed) {
      router.replace("/chat");
    }

  if (status !== "signed-in" || !allowed) {
    return <LoadingScreen />;
  }

  return <>{children}</>;
}
