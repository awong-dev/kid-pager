"use client";

import dynamic from "next/dynamic";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Link from "@mui/material/Link";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import RoomIcon from "@mui/icons-material/Room";

import { formatRelativeAge } from "@/lib/time";
import type { LocationMapTrailPoint } from "@/components/LocationMap";

// Leaflet touches `window` at import time; the static export prerenders with no DOM.
const LocationMap = dynamic(() => import("@/components/LocationMap"), {
  ssr: false,
  loading: () => <Skeleton variant="rectangular" height={200} sx={{ borderRadius: "8px" }} />,
});

/** A location fix as a small Leaflet map plus text lat/lon, accuracy, age, source and map links. */
export default function LocationCard({
  lat,
  lon,
  accM,
  fixTsMs,
  src,
  cached,
  trail,
  title = "Last known location",
}: {
  lat: number;
  lon: number;
  accM?: number | null;
  fixTsMs: number;
  /** `"gnss" | "cell"`; typed loosely so an unknown source still renders. */
  src?: string | null;
  /** Present on `LocationFixDoc` only; omitted from the card when unknown. */
  cached?: boolean | null;
  /** Recent fixes, oldest first, drawn as a faint trail ending at this point. */
  trail?: LocationMapTrailPoint[];
  title?: string;
}) {
  const coords = `${lat.toFixed(5)},${lon.toFixed(5)}`;
  const googleUrl = `https://www.google.com/maps/search/?api=1&query=${coords}`;
  const appleUrl = `https://maps.apple.com/?q=${coords}`;
  const coarse = isCoarseFix(src, accM);
  const stale = isStaleFix(fixTsMs);

  return (
    <Card variant="outlined" sx={{ maxWidth: 360 }}>
      <CardContent>
          <RoomIcon color="action" fontSize="small" />
          <Typography variant="subtitle2">{title}</Typography>
        </Stack>

        <Stack sx={{ mt: 1, mb: 1 }}>
          <LocationMap
            lat={lat}
            lon={lon}
            accM={accM}
            src={src}
            trail={trail && trail.length > 1 ? trail : undefined}
          />
        </Stack>

        <Typography variant="body2" sx={{ mt: 0.5 }}>
          {lat.toFixed(5)}, {lon.toFixed(5)}
          {accM != null ? ` (±${accM} m)` : ""}
        </Typography>
        {coarse && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {src === "cell" ? "Approximate, from the cell tower." : "Approximate -- low-accuracy fix."}
          </Typography>
        )}
        <Typography
          variant="caption"
          color={stale ? "warning.main" : "text.secondary"}
          sx={{ display: "block", fontWeight: stale ? 600 : 400 }}
        >
          {formatRelativeAge(fixTsMs)}
          {cached ? " (cached)" : ""}
        </Typography>

          <Link href={googleUrl} target="_blank" rel="noopener noreferrer">
            Open in Google Maps
          </Link>
          <Link href={appleUrl} target="_blank" rel="noopener noreferrer">
            Open in Apple Maps
          </Link>
        </Stack>
      </CardContent>
    </Card>
  );
}
