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
export default function RequireAuth({
  children,
  requireAdmin = false,
}: {
  children: ReactNode;
  requireAdmin?: boolean;
}) {
  const router = useRouter();

  useEffect(() => {
    if (status === "signed-out" || status === "not-registered") {
      router.replace("/chat");
    }

    return <LoadingScreen />;
  }

  return <>{children}</>;
}
