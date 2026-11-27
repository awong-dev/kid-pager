"use client";

/** Pieces shared by both Move screens: state chips, the summary line, a status row, the
 * "what is down" list and a heading with a step number. */

import type { ReactNode } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutlined";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutlined";
import HourglassEmptyIcon from "@mui/icons-material/HourglassEmpty";
import BlockIcon from "@mui/icons-material/Block";
import SyncIcon from "@mui/icons-material/Sync";

import { DOWNTIME, type ChipSpec, type Tally, tallyText } from "@/lib/move";

const MARKS: Record<ChipSpec["mark"], ReactNode> = {
  done: <CheckCircleOutlineIcon />,
  wait: <HourglassEmptyIcon />,
  work: <SyncIcon />,
  problem: <ErrorOutlineIcon />,
  stop: <BlockIcon />,
};

/** A state chip: the label says the state, the icon repeats it, the colour is the third cue. */
export function StateChip({ spec, label }: { spec: ChipSpec; label?: string }) {
  return (
    <Chip
      size="small"
      icon={MARKS[spec.mark] as React.ReactElement}
      label={label ?? spec.label}
      color={spec.color}
      variant={spec.filled ? "filled" : "outlined"}
    />
  );
}

export function SummaryLine({ tally, noun = "pager" }: { tally: Tally; noun?: string }) {
  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }} role="status">
        {tallyText(tally, noun)}
      </Typography>
      {tally.needsPerson > 0 && (
        <Typography variant="body2" color="error.main">
          {tally.needsPerson} need{tally.needsPerson === 1 ? "s" : ""} a person to act (marked below).
        </Typography>
      )}
    </Box>
  );
}

/** One pager (or bridge) in a status list; wraps at phone width. */
export function StatusRow({
  title,
  subtitle,
  chips,
  facts,
  detail,
  remedy,
  actions,
}: {
  title: string;
  subtitle?: string | null;
  chips: ReactNode;
  facts?: (string | null | false)[];
  detail?: string | null;
  remedy?: ReactNode;
  actions?: ReactNode;
}) {
  const shown = (facts ?? []).filter((f): f is string => !!f);
  return (
    <Box sx={{ py: 1.5, borderTop: 1, borderColor: "divider" }}>
      <Stack direction="row" useFlexGap spacing={1} sx={{ flexWrap: "wrap", alignItems: "center" }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, overflowWrap: "anywhere" }}>
          {title}
        </Typography>
        {subtitle && (
          <Typography variant="body2" color="text.secondary">
            {subtitle}
          </Typography>
        )}
        <Stack direction="row" useFlexGap spacing={0.75} sx={{ flexWrap: "wrap", alignItems: "center" }}>
          {chips}
        </Stack>
      </Stack>
      {shown.length > 0 && (
        <Typography variant="caption" color="text.secondary" component="div" sx={{ mt: 0.5 }}>
          {shown.join(" · ")}
        </Typography>
      )}
      {detail && (
        <Typography variant="body2" sx={{ mt: 0.5 }}>
          {detail}
        </Typography>
      )}
      {remedy && (
        <Box sx={{ mt: 1, pl: 1.5, py: 0.5, borderLeft: 4, borderColor: "warning.main", bgcolor: "action.hover" }}>
          {remedy}
        </Box>
      )}
      {actions && (
        <Stack direction="row" useFlexGap spacing={1} sx={{ mt: 1, flexWrap: "wrap" }}>
          {actions}
        </Stack>
      )}
    </Box>
  );
}

export function Section({
  title,
  step,
  children,
  hint,
}: {
  title: string;
  step?: number;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <Card variant="outlined" component="section">
      <CardContent>
        <Stack spacing={1.5}>
          <Typography variant="h6" component="h2">
            {step ? `${step}. ` : ""}
            {title}
          </Typography>
          {hint && (
            <Typography variant="body2" color="text.secondary">
              {hint}
            </Typography>
          )}
          {children}
        </Stack>
      </CardContent>
    </Card>
  );
}

/** What is down for whom, in plain words (docs/MOVE_DESIGN.md §1). */
export function DowntimeList({ frozen }: { frozen: boolean }) {
  return (
    <Alert severity="warning" icon={false}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        {frozen ? "Down right now, and for whom" : "What will be down once you press Freeze"}
      </Typography>
      <Stack component="ul" spacing={0.75} sx={{ m: 0, mt: 0.5, pl: 2.5 }}>
        {DOWNTIME.map((d) => (
          <li key={d.who}>
            <Typography variant="body2">
              <strong>{d.who}.</strong> {d.text}
            </Typography>
          </li>
        ))}
      </Stack>
      <Typography variant="body2" sx={{ mt: 1 }}>
        Nothing sent is lost: it is refused or held, and arrives on the new server.
      </Typography>
    </Alert>
  );
}
