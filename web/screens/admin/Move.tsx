"use client";

/** `/admin/move` (super only): the screen depends on this relay's side of the move. */

import Alert from "@mui/material/Alert";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import ImportCard from "@/components/move/ImportCard";
import SourceMoveFrozen from "@/components/move/SourceMoveFrozen";
import SourceMoveStart from "@/components/move/SourceMoveStart";
import TargetMove from "@/components/move/TargetMove";
import { ApiError } from "@/lib/api";
import { type AdminMove, getAdminMove, getOwnMoveInfo } from "@/lib/move";
import { usePoll } from "@/lib/usePoll";

import { LoadingScreen } from "@/components/RequireAuth";
import { useEffect, useState } from "react";

function Idle({ onChanged }: { onChanged: () => void }) {
  const [empty, setEmpty] = useState<boolean | null>(null);
  useEffect(() => {
    void getOwnMoveInfo().then((i) => setEmpty(i ? i.empty : null));
  }, []);
  const importCard = <ImportCard onImported={onChanged} />;
  return (
    <Stack spacing={3}>
      {empty && importCard}
      <Stack spacing={2}>
        <Typography variant="h6" component="h2">
          Move this server to another
        </Typography>
        <SourceMoveStart onFrozen={onChanged} />
      </Stack>
      {empty !== true && (
        <Stack spacing={2}>
          {empty === false && (
            <Alert severity="info">This server already holds families, so it cannot take an import. To receive a move, start from an empty server.</Alert>
          )}
          {importCard}
        </Stack>
      )}
    </Stack>
  );
}

function View({ move, onChanged }: { move: AdminMove; onChanged: () => void }) {
  if (move.role === "source" && (move.state === "frozen" || move.state === "cutover")) {
    return <SourceMoveFrozen move={move} onChanged={onChanged} />;
  }
  if (move.role === "target" && move.state === "importing") {
    return (
      <Stack spacing={2}>
        <Alert severity="warning">
          <strong>The import did not finish.</strong> Run the import again with the same bundle and passphrase. Nothing else
          can be done on this server until it finishes.
        </Alert>
        <ImportCard onImported={onChanged} />
      </Stack>
    );
  }
  if (move.role === "target") return <TargetMove move={move} onChanged={onChanged} />;
  return (
    <Stack spacing={2}>
      {move.role === "source" && (
        <Alert severity="info">
          {move.state === "cancelled" ? "The last move was cancelled. This server is live again." : "The last move is finished."}
        </Alert>
      )}
      <Idle onChanged={onChanged} />
    </Stack>
  );
}

export default function MovePage() {
  const { data, error, reload } = usePoll(getAdminMove, 5_000);
  return (
    <Stack spacing={3}>
      <Typography variant="h5" component="h1">
        Move
      </Typography>
      {error instanceof ApiError && <Alert severity="error">{typeof error.detail === "string" ? error.detail : error.message}</Alert>}
      {data ? <View move={data} onChanged={() => void reload()} /> : !error && <LoadingScreen />}
    </Stack>
  );
}
