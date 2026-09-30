"use client";


import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { ApiError, api } from "@/lib/api";
import { useDirectory } from "@/lib/directory";
import { familyQuery } from "@/lib/family-context";

interface PersonRow {
  message: boolean;
  locate: boolean;
}

  phone: string;
}

export default function ApprovedEditor({
  uid,
  familyId,
  onSaved,
}: {
  uid: string;
  familyId: string | null;
  onSaved?: () => void;
}) {
  const directory = useDirectory();
  const [people, setPeople] = useState<Map<string, PersonRow>>(new Map());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
  }, [uid]);

  const peopleContacts = directory.contacts.filter((c) => c.uid !== uid);

    setPeople((prev) => {
      const next = new Map(prev);
      if (existing?.message) {
      } else {
      }
      return next;
    });
  }

    setPeople((prev) => {
      const next = new Map(prev);
      if (!existing) return prev;
      return next;
    });
  }

    });
  }

  }

  async function save() {
    setError(null);
    const peoplePayload = Array.from(people.entries())
      .filter(([, row]) => row.message)
    setBusy(true);
    try {
        people: peoplePayload,
      });
      onSaved?.();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail ?? err.message) : "Failed to save approved list");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}

      <Typography variant="subtitle2">People</Typography>
      <Stack spacing={0.5}>
        {peopleContacts.map((c) => {
          const entry = directory.byUid(c.uid);
          const crossFamily = entry?.familyId !== familyId;
          return (
              <FormControlLabel
                sx={{ flexGrow: 1, mr: 0 }}
                control={
                  <Checkbox
                    size="small"
                    checked={row?.message ?? false}
                  />
                }
                label={`@${c.alias}`}
              />
              <Tooltip title={crossFamily ? "Location stays within a family" : ""}>
                <span>
                  <FormControlLabel
                    control={
                      <Switch
                        size="small"
                        checked={row?.locate ?? false}
                        disabled={!row?.message || crossFamily}
                      />
                    }
                    label="Locate"
                  />
                </span>
              </Tooltip>
            </Stack>
          );
        })}
        {peopleContacts.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            No other people known to this family yet.
          </Typography>
        )}
      </Stack>

      <Divider />

      <Stack spacing={1}>
        <Typography variant="caption" color="text.secondary">
        </Typography>
      </Stack>

      <Button variant="contained" onClick={() => void save()} disabled={busy} sx={{ alignSelf: "flex-start" }}>
        Save approved list
      </Button>
    </Stack>
  );
}
