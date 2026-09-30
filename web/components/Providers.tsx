"use client";

import CssBaseline from "@mui/material/CssBaseline";
import { ThemeProvider } from "@mui/material/styles";
import { AppRouterCacheProvider } from "@mui/material-nextjs/v16-appRouter";
import type { ReactNode } from "react";

import { AuthProvider } from "@/lib/auth-context";
import { DirectoryProvider } from "@/lib/directory";
import { FamilyProvider } from "@/lib/family-context";
import theme from "@/lib/theme";

/** Every provider the app needs. `FamilyProvider` sits inside `AuthProvider` and outside
 * `DirectoryProvider`. */
export default function Providers({ children }: { children: ReactNode }) {
  return (
    <AppRouterCacheProvider options={{ key: "mui" }}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <AuthProvider>
          <FamilyProvider>
            <DirectoryProvider>{children}</DirectoryProvider>
          </FamilyProvider>
        </AuthProvider>
      </ThemeProvider>
    </AppRouterCacheProvider>
  );
}
