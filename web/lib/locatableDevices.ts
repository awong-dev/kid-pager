"use client";

/**
 * Live devices whose location this account may see, merged by id (own, then `locate` edge, then
 * family-wide for a family admin). Queries filter on `familyId` for `firestore.rules`.
 */

import { useMemo } from "react";

import { useAuth } from "./auth-context";
import { useFamily } from "./family-context";
import { useLive } from "./live";
import {
  familyDevicesLocatableByQuery,
  familyDevicesOwnedByQuery,
  familyDevicesQuery,
} from "./live/queries";
import type { DeviceDoc } from "./types";

export interface LocatableDevice extends DeviceDoc {
  id: string;
  /** True when the viewer owns this device (listed as "You"). */
  isOwn: boolean;
  /** True when "Locate now" is enabled: owner, edge holder, or a plain family admin (not super). */
  canLocate: boolean;
}

export function useLocatableDevices(): { devices: LocatableDevice[]; loaded: boolean; failed: boolean } {
  const { me, isFamilyAdmin, isSuper } = useAuth();
  const { familyId } = useFamily();
  const ready = Boolean(me && familyId);
  const grantedLive = useLive<DeviceDoc>(
    ready ? familyDevicesLocatableByQuery(familyId!, me!.uid) : null
  );
  const ownLive = useLive<DeviceDoc>(ready ? familyDevicesOwnedByQuery(familyId!, me!.uid) : null);
  const familyLive = useLive<DeviceDoc>(
    ready && isFamilyAdmin ? familyDevicesQuery(familyId!) : null
  );
  const granted = useMemo<LocatableDevice[]>(
    () => grantedLive.docs.map((d) => ({ id: d.id, ...d.data, isOwn: false, canLocate: true })),
    [grantedLive.docs]
  );
  const own = useMemo<LocatableDevice[]>(
    () => ownLive.docs.map((d) => ({ id: d.id, ...d.data, isOwn: true, canLocate: true })),
    [ownLive.docs]
  );
  const familyWide = useMemo<LocatableDevice[]>(
    () =>
      familyLive.docs.map((d) => ({
        id: d.id,
        ...d.data,
        isOwn: false,
        // A super needs ownership or an edge, merged in below with priority.
        canLocate: !isSuper,
      })),
    [familyLive.docs, isSuper]
  );
  const grantedLoaded = ready && !grantedLive.loading;
  const ownLoaded = ready && !ownLive.loading;
  const familyLoaded = ready && isFamilyAdmin && !familyLive.loading;

  const devices = useMemo(() => {
    const byId = new Map<string, LocatableDevice>();
    for (const d of familyWide) byId.set(d.id, d);
    for (const d of granted) byId.set(d.id, d);
    for (const d of own) byId.set(d.id, d);
    return [...byId.values()];
  }, [own, granted, familyWide]);

  const failed = Boolean(grantedLive.error || ownLive.error || familyLive.error);
  return {
    devices,
    loaded: grantedLoaded && ownLoaded && (!isFamilyAdmin || familyLoaded),
    failed,
  };
}
