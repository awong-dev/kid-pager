"use client";

/** Live data the shell and the Home screen both need, subscribed once here (`useLive` does not
 * dedupe identical queries): the open-alert count and the devices this account may see.
 * Rendered by `AppShell`, so every authenticated screen can read it. */

import { type ReactNode, createContext, useContext, useMemo } from "react";

import { useAuth } from "@/lib/auth-context";
import { useFamily } from "@/lib/family-context";
import { useLive } from "@/lib/live";
import { openAlertsQuery } from "@/lib/live/queries";
import { type LocatableDevice, useLocatableDevices } from "@/lib/locatableDevices";

export interface OpenAlerts {
  count: number;
  /** No snapshot yet (and a family to ask about). */
  loading: boolean;
  failed: boolean;
}

export interface ShellDevices {
  devices: LocatableDevice[];
  loaded: boolean;
  failed: boolean;
}

interface ShellData {
  alerts: OpenAlerts;
  devices: ShellDevices;
}

const Ctx = createContext<ShellData | null>(null);

export function ShellDataProvider({ children }: { children: ReactNode }) {
  const { isFamilyAdmin } = useAuth();
  const { familyId } = useFamily();
  // Open alerts are readable by super or that family's admin only (firestore.rules).
  const live = useLive(isFamilyAdmin && familyId ? openAlertsQuery(familyId) : null);
  const { devices, loaded, failed } = useLocatableDevices();
  const value = useMemo<ShellData>(
    () => ({
      // with no family in scope `useLive` keeps the last count; show none
      alerts: {
        count: isFamilyAdmin && familyId ? live.docs.length : 0,
        loading: live.loading,
        failed: live.error !== null,
      },
      devices: { devices, loaded, failed },
    }),
    [isFamilyAdmin, familyId, live.docs.length, live.loading, live.error, devices, loaded, failed],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

function useShellData(): ShellData {
  const v = useContext(Ctx);
  if (!v) throw new Error("useShellData must be used inside AppShell");
  return v;
}

/** Live open-alert count (0 for non-admins). Shared with the shell's badges. */
export function useOpenAlerts(): OpenAlerts {
  return useShellData().alerts;
}
export function useOpenAlertCount(): number {
  return useShellData().alerts.count;
}

/** Devices this account may see (own, `locate` grants, the whole family for an admin). */
export function useShellDevices(): ShellDevices {
  return useShellData().devices;
}
