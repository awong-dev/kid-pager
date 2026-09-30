"use client";

/**
 * Live devices whose location this account may see, merged by id (own, then `locate` edge, then
 * family-wide for a family admin). Queries filter on `familyId` for `firestore.rules`.
 */


import { useAuth } from "./auth-context";
import { useFamily } from "./family-context";
import type { DeviceDoc } from "./types";

export interface LocatableDevice extends DeviceDoc {
  id: string;
  /** True when the viewer owns this device (listed as "You"). */
  isOwn: boolean;
  /** True when "Locate now" is enabled: owner, edge holder, or a plain family admin (not super). */
  canLocate: boolean;
}

export function useLocatableDevices(): { devices: LocatableDevice[]; loaded: boolean } {
  const { me, isFamilyAdmin, isSuper } = useAuth();
  const { familyId } = useFamily();

  const devices = useMemo(() => {
    const byId = new Map<string, LocatableDevice>();
    for (const d of familyWide) byId.set(d.id, d);
    for (const d of granted) byId.set(d.id, d);
    for (const d of own) byId.set(d.id, d);
    return [...byId.values()];
  }, [own, granted, familyWide]);

  return { devices, loaded: grantedLoaded && ownLoaded && (!isFamilyAdmin || familyLoaded) };
}
