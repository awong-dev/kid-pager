/** GNSS enable flag: `GET`/`PUT /api/devices/{id}/gnss`. */

export interface GnssConfigResponse {
  en: boolean;
  pending: boolean; // cfg.loc pushed, not yet acked `shown`
  reported: number | null; // last /status `gnss` (0/1); null = not reported
}

export interface GnssPutRequest {
  en: boolean;
}

export function reportedLabel(reported: number | null): string {
  if (reported === 1) return "on";
  if (reported === 0) return "off";
  return "unknown";
}
