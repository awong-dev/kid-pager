/** Helpers for the OTA firmware UI. */

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { DeviceStatusDoc } from "@/lib/types";

/** Monthly cellular data target, 10 MB. */
export const MONTHLY_TARGET_BYTES = 10_000_000;

export interface FirmwareBuild {
  id16: string;
  version: string;
  size: number;
  published: number; // epoch seconds
  kind: "full" | "delta";
  osz: number;
  estBytes: number;
}

export interface OtaPushResult {
  ok: boolean;
  kind?: "full" | "delta";
  osz?: number;
  estBytes?: number;
}

type OtaStatus = Pick<DeviceStatusDoc, "otaState" | "otaPct" | "otaErr">;

/** One-line job state for the Firmware chip; null when no job reported. */
export function otaStateLabel(status?: OtaStatus | null): string | null {
  switch (status?.otaState) {
    case "wait":
      return "Update queued";
    case "dl":
      return `Downloading ${status.otaPct ?? 0} %`;
    case "ready":
      return "Ready, installs when idle";
    case "inst":
      return "Installing…";
    case "ok":
      return "Updated";
    case "fail":
      return `Update failed (${status.otaErr ?? "unknown"})`;
    case "rb":
      return `Rolled back (${status.otaErr ?? "boot"})`;
    default:
      return null;
  }
}

/** 1000-based: "339 KB", "1.1 MB". */
export function formatBytes(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`;
  if (n >= 1000) return `${Math.round(n / 1000)} KB`;
  return `${n} B`;
}

  return r.builds;
}

  const [newest, setNewest] = useState<FirmwareBuild | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
      .then((b) => {
        if (!cancelled) setNewest(b[0]);
      })
      .catch(() => {
        // Chip falls back to the job-state label without a newest build.
      });
    return () => {
      cancelled = true;
    };
  return newest;
}

}

}
