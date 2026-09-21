"use client";

/**
 * Leaflet map for one location fix; import only via `next/dynamic` with `ssr: false`
 * (Leaflet touches `window` at load). Markers use `L.divIcon` because `L.Icon.Default`'s
 * icon URLs break under webpack (Leaflet issue 4968).
 */

import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { useEffect, useRef } from "react";

import { isCoarseFix, zoomForAccuracy } from "@/lib/location";
import { OSM_ATTRIBUTION, OSM_TILE_MAX_ZOOM, OSM_TILE_URL } from "@/lib/mapTiles";

// Used only to pick a zoom level, not for layout.
const VIEWPORT_PX_ESTIMATE = 260;

const PRECISE_COLOR = "#2f5d8a"; // theme.ts primary.main
const TRAIL_COLOR = "#8a5d2f"; // theme.ts secondary.main, doubles as the "coarse" colour

export interface LocationMapTrailPoint {
  lat: number;
  lon: number;
}

export default function LocationMap({
  lat,
  lon,
  accM,
  src,
  trail,
}: {
  lat: number;
  lon: number;
  accM?: number | null;
  src?: string | null;
  trail?: LocationMapTrailPoint[];
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const coarse = isCoarseFix(src, accM);
    const zoom = zoomForAccuracy(accM, VIEWPORT_PX_ESTIMATE, lat);

    const map = L.map(el, {
      center: [lat, lon],
      zoom,
      // Scroll-wheel zoom stays off until the map is tapped, so it does not hijack page scroll.
      scrollWheelZoom: false,
    });

    const enableScroll = () => map.scrollWheelZoom.enable();
    const disableScroll = () => map.scrollWheelZoom.disable();
    el.addEventListener("click", enableScroll);
    el.addEventListener("focus", enableScroll, true);
    el.addEventListener("mouseleave", disableScroll);
    el.addEventListener("blur", disableScroll, true);

    L.tileLayer(OSM_TILE_URL, {
      maxZoom: OSM_TILE_MAX_ZOOM,
      attribution: OSM_ATTRIBUTION,
    }).addTo(map);

    // Faint trail of recent fixes, oldest to newest.
    if (trail && trail.length > 1) {
      L.polyline(
        trail.map((p): [number, number] => [p.lat, p.lon]),
        { color: TRAIL_COLOR, weight: 2, opacity: 0.35, dashArray: "4 6" }
      ).addTo(map);
      trail.slice(0, -1).forEach((p, i) => {
        L.circleMarker([p.lat, p.lon], {
          radius: 3,
          weight: 0,
          fillColor: TRAIL_COLOR,
          // Older points fade out; the one right before "now" is the most opaque.
          fillOpacity: 0.15 + (0.35 * (i + 1)) / trail.length,
        }).addTo(map);
      });
    }

    if (accM != null && accM > 0) {
      L.circle([lat, lon], {
        radius: accM,
        color: coarse ? TRAIL_COLOR : PRECISE_COLOR,
        weight: 1,
        fillColor: coarse ? TRAIL_COLOR : PRECISE_COLOR,
        fillOpacity: coarse ? 0.25 : 0.15,
      }).addTo(map);
    }

    if (coarse) {
      // Coarse fix: a small muted dot, not a pin, so it does not claim precision.
      L.marker([lat, lon], {
        icon: L.divIcon({
          className: "pager-location-marker pager-location-marker--coarse",
          html: `<span style="display:block;width:8px;height:8px;border-radius:50%;background:${TRAIL_COLOR};opacity:0.6"></span>`,
          iconSize: [8, 8],
          iconAnchor: [4, 4],
        }),
        keyboard: false,
        interactive: false,
      }).addTo(map);
    } else {
      L.marker([lat, lon], {
        icon: L.divIcon({
          className: "pager-location-marker",
          html: `<span style="display:block;width:14px;height:14px;border-radius:50%;background:${PRECISE_COLOR};border:2px solid #fff;box-shadow:0 0 2px rgba(0,0,0,0.6)"></span>`,
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        }),
        keyboard: false,
      }).addTo(map);
    }

    // Leaflet's cached size can be stale on first paint; recheck once mounted and on resize.
    const invalidate = () => map.invalidateSize();
    const raf = requestAnimationFrame(invalidate);
    window.addEventListener("resize", invalidate);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", invalidate);
      el.removeEventListener("click", enableScroll);
      el.removeEventListener("focus", enableScroll, true);
      el.removeEventListener("mouseleave", disableScroll);
      el.removeEventListener("blur", disableScroll, true);
      map.remove();
    };
    // The whole map is rebuilt on any change.
  }, [lat, lon, accM, src, trail]);

  return (
    <div
      ref={containerRef}
      aria-label="Map of the pager's last known location"
    />
  );
}
