import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import RoomIcon from "@mui/icons-material/Room";

import { formatRelativeAge } from "@/lib/time";

export default function LocationCard({
  lat,
  lon,
  accM,
  fixTsMs,
  title = "Last known location",
}: {
  lat: number;
  lon: number;
  accM?: number | null;
  fixTsMs: number;
  title?: string;
}) {
  const coords = `${lat.toFixed(5)},${lon.toFixed(5)}`;
  const googleUrl = `https://www.google.com/maps/search/?api=1&query=${coords}`;
  const appleUrl = `https://maps.apple.com/?q=${coords}`;

  return (
    <Card variant="outlined" sx={{ maxWidth: 360 }}>
      <CardContent>
          <RoomIcon color="action" fontSize="small" />
          <Typography variant="subtitle2">{title}</Typography>
        </Stack>
        <Typography variant="body2" sx={{ mt: 0.5 }}>
          {lat.toFixed(5)}, {lon.toFixed(5)}
          {accM != null ? ` (±${accM} m)` : ""}
        </Typography>
          {formatRelativeAge(fixTsMs)}
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
