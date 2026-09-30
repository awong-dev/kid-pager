/** Pure helpers for rendering a location fix: zoom, staleness, coarseness and the timeline. */


// A fix older than this reads as stale in `LocationCard`.
export const STALE_FIX_MS = 30 * 60 * 1000;

export function isStaleFix(fixTsMs: number, nowMs: number = Date.now()): boolean {
  return nowMs - fixTsMs > STALE_FIX_MS;
}

// An accuracy radius at or above this is too coarse to draw as a precise
// point, regardless of source.
export const COARSE_ACCURACY_M = 500;

/** True if the fix is approximate: any cell fix, or any fix with `accM >= COARSE_ACCURACY_M`. */
export function isCoarseFix(src: string | null | undefined, accM: number | null | undefined): boolean {
  return src === "cell" || (accM != null && accM >= COARSE_ACCURACY_M);
}

const EARTH_CIRCUMFERENCE_M = 40_075_016.686; // WGS84 equatorial circumference
const TILE_SIZE_PX = 256; // Leaflet/OSM's slippy-map tile size
const MIN_ZOOM = 2;
const MAX_ZOOM = 19; // OSM's raster tiles stop serving beyond this
const DEFAULT_ZOOM_NO_ACCURACY = 16; // a plausible "close in" zoom when accuracy is unknown
// Fraction of the map's shorter side the accuracy circle's diameter should occupy.
const TARGET_CIRCLE_FRACTION = 0.6;

/** Integer zoom (clamped to OSM's range) framing an accuracy circle of radius `accM` metres at
 * latitude `lat`, by the Web Mercator metres-per-pixel formula; `viewportPx` is a rough size. */
export function zoomForAccuracy(
  accM: number | null | undefined,
  viewportPx = 260,
  lat = 0
): number {
  if (accM == null || accM <= 0) return DEFAULT_ZOOM_NO_ACCURACY;
  const targetDiameterPx = viewportPx * TARGET_CIRCLE_FRACTION;
  const metersPerPixel = (2 * accM) / targetDiameterPx;
  const z = Math.log2(
    (EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180)) / (TILE_SIZE_PX * metersPerPixel)
  );
  if (!Number.isFinite(z)) return DEFAULT_ZOOM_NO_ACCURACY;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(z)));
}

/** Display words for a fix source. */
export function sourceLabel(src: string | null | undefined): string {
  return src === "gnss" ? "GPS" : src === "cell" ? "cell tower" : (src ?? "unknown source");
}

// The pager's `why` values mapped to timeline words.
const WHY_LABELS: Record<string, string> = {
  still: "hourly check-in",
  move: "moving",
  stop: "stopped",
  cell: "cell fix",
  gnss: "GPS fix",
};

/** Label for a timeline entry's `why`/`reqId` (`reqId` wins); an unknown `why` shows raw. */
export function whyLabel(
  why: string | null | undefined,
  reqId: string | null | undefined
): string | null {
  if (reqId) return "requested";
  if (!why) return null;
  return WHY_LABELS[why] ?? why;
}


/** One timeline row: a stored fix, or the synthetic "position unknown" row (`positionUnknown`). */
export interface LocationTimelineEntry {
  id: string;
  /** `LocationFixDoc.ts` (or `lastCell.ts`) in ms; start of a merged dwell range. */
  startMs: number;
  /** `LocationFixDoc.lastTs` in ms, set when a later report extended the dwell; else `null`. */
  endMs: number | null;
  lat: number | null;
  lon: number | null;
  accM: number | null;
  src: string | null;
  why: string | null;
  reqId: string | null;
  cached: boolean;
  positionUnknown: boolean;
}

export interface LocationFixRow extends LocationFixDoc {
  id: string;
}

/**
 * Timeline rows from `locations` docs, newest first, within `windowDays`. A `lastCell.ts` newer
 * than the newest fix adds a "position unknown" row (the relay stores only resolved fixes).
 */
export function buildLocationTimeline(
  fixes: LocationFixRow[],
  lastCellTsS: number | null | undefined,
): LocationTimelineEntry[] {

  const entries: LocationTimelineEntry[] = [];
  for (const f of fixes) {
    const startMs = f.ts * 1000;
    const endMs = f.lastTs != null ? f.lastTs * 1000 : null;
    if ((endMs ?? startMs) < cutoffMs) continue;
    entries.push({
      id: f.id,
      startMs,
      endMs,
      lat: f.lat,
      lon: f.lon,
      accM: f.accM,
      src: f.src,
      why: f.why ?? null,
      reqId: f.reqId ?? null,
      cached: f.cached,
      positionUnknown: false,
    });
  }
  entries.sort((a, b) => (b.endMs ?? b.startMs) - (a.endMs ?? a.startMs));

  if (lastCellTsS != null) {
    const lastCellMs = lastCellTsS * 1000;
    const newestFixMs = entries[0] ? (entries[0].endMs ?? entries[0].startMs) : 0;
    if (lastCellMs > newestFixMs && lastCellMs >= cutoffMs) {
      entries.unshift({
        id: `lastCell-${lastCellTsS}`,
        startMs: lastCellMs,
        endMs: null,
        lat: null,
        lon: null,
        accM: null,
        src: "cell",
        why: null,
        reqId: null,
        cached: false,
        positionUnknown: true,
      });
    }
  }

  return entries;
}

export interface LocationTimelineDayGroup {
  dayKey: string;
  entries: LocationTimelineEntry[];
}

/** Groups a newest-first timeline by local calendar day, keeping order. */
export function groupTimelineByDay(entries: LocationTimelineEntry[]): LocationTimelineDayGroup[] {
  const groups = new Map<string, LocationTimelineEntry[]>();
  for (const e of entries) {
    const d = new Date(e.startMs);
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const list = groups.get(key);
    if (list) list.push(e);
    else groups.set(key, [e]);
  }
  return Array.from(groups.entries()).map(([dayKey, es]) => ({ dayKey, entries: es }));
}
