"use client";

import { stripBase } from "@/lib/basePath";
import { type ReactNode, useEffect } from "react";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";

import { useNavigate } from "@/lib/router";
import { useAuth } from "@/lib/auth-context";
import { stashReturnPath } from "@/lib/returnPath";

export function LoadingScreen() {
  return (
    <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", height: "60vh" }}>
      <CircularProgress />
    </Box>
  );
}

export type Role = "member" | "admin" | "super";

/** Client-side route guard (UX only; the relay and rules enforce access). Rendered once, by the
 * router (`app/page.tsx`), around every authenticated screen.
 * `role`: `member` = any signed-in user; `admin` = a family admin or super; `super` = super only. */
export default function RequireAuth({ children, role }: { children: ReactNode; role: Role }) {
  const { status, isFamilyAdmin, isSuper, needsSetup } = useAuth();
  const navigate = useNavigate();
  const allowed = role === "member" ? true : role === "super" ? isSuper : isFamilyAdmin;

  useEffect(() => {
    if (status === "signed-out" || status === "not-registered") {
      // Home backend with no users yet: first run. Keep the page asked for (and its #fragment).
      stashReturnPath(`${stripBase(window.location.pathname)}${window.location.search}${window.location.hash}`);
      navigate.replace(needsSetup ? "/setup" : "/login");
    } else if (status === "signed-in" && !allowed) {
      navigate.replace("/chat");
    }
  }, [status, allowed, navigate, needsSetup]);

  if (status !== "signed-in" || !allowed) {
    return <LoadingScreen />;
  }

  return <>{children}</>;
}
