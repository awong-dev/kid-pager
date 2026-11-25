"use client";

/** `/family/permissions`: every control that decides who may talk to whom. The member list
 * (`PermissionsOverview`) shows each person's rules at a glance; choosing one (`?who=<alias>`, so it
 * can be linked and survives reload and Back) shows `PersonPermissions` beside it on md and up, or
 * instead of it on a phone. The split is CSS only. Defaults and the links to related controls sit
 * around it. */

import { useMemo } from "react";
import Alert from "@mui/material/Alert";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { Link, useSearch } from "@/lib/router";
import PermissionsOverview from "./PermissionsOverview";
import { AlsoAffectsSection, DefaultsSection } from "./PermissionsLinks";
import PersonPermissions from "./PersonPermissions";
import { PERMISSIONS_PATH, locateViewers, selectedPerson, tallyLines, whoOf } from "./permissionsModel";
import { useApprovedSummaries } from "./useApprovedSummaries";
import { useFamilyMembers } from "./useFamilyMembers";

export default function PermissionsPage() {
  const { members, loading, error } = useFamilyMembers();
  const search = useSearch();
  const people = useMemo(() => members.filter((m) => m.kind === "person"), [members]);
  const who = whoOf(search);
  const selected = selectedPerson(people, search);
  const summaries = useApprovedSummaries(people);
  // Phones show the list or the person, never both; md and up show both.
  const showList = { xs: who ? "none" : "block", md: "block" } as const;
  const showDetail = { xs: who ? "block" : "none", md: "block" } as const;

  return (
    <Stack spacing={4}>
      <Stack spacing={1}>
        <Typography variant="h5" component="h1">
          Permissions
        </Typography>
        <Typography variant="body1" color="text.secondary">
          Who each person can message, and who can message them.
        </Typography>
      </Stack>

      <Box sx={{ display: showList }}>
        <DefaultsSection />
      </Box>

      {error && <Alert severity="error">Could not load your family members. Try reloading the page.</Alert>}
      {loading && !error && <Skeleton variant="rounded" height={160} />}

      {!loading && !error && (
        <Box
          sx={{
            display: { xs: "block", md: "grid" },
            gridTemplateColumns: { md: "minmax(280px, 360px) minmax(0, 1fr)" },
            gap: 4,
            alignItems: "start",
          }}
        >
          <Box sx={{ display: showList }}>
            <PermissionsOverview people={people} selectedUid={selected?.uid ?? null} approved={summaries.byAlias} />
          </Box>

          <Box sx={{ display: showDetail, minWidth: 0 }}>
            {who && (
              <Button
                component={Link}
                href={PERMISSIONS_PATH}
                startIcon={<ArrowBackIcon />}
                sx={{ alignSelf: "flex-start", display: { md: "none" }, mb: 1 }}
              >
                All people
              </Button>
            )}
            {who && !selected && (
              <Alert severity="warning" sx={{ mb: 2 }}>
                No one in your family has the alias @{who}. Pick someone from the list.
              </Alert>
            )}
            {selected ? (
              <PersonPermissions
                key={selected.uid}
                member={selected}
                viewers={locateViewers(summaries.byAlias, selected.alias)}
                viewersLoading={summaries.loading}
                onApprovedSaved={() => summaries.refresh(selected.alias)}
              />
            ) : (
              <Stack spacing={1} sx={{ display: { xs: "none", md: "flex" } }}>
                <Typography variant="h6" component="h2">
                  Pick someone
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Choose a person on the left to see and change who they can message and who can message them.
                </Typography>
                {tallyLines(people.map((p) => p.policy)).map((l) => (
                  <Typography key={l} variant="body2">
                    {l}
                  </Typography>
                ))}
              </Stack>
            )}
          </Box>
        </Box>
      )}

      <Box sx={{ display: showList }}>
        <AlsoAffectsSection />
      </Box>
    </Stack>
  );
}
