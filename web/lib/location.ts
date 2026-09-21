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
