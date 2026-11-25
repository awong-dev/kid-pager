"use client";

import { useState } from "react";
import Chip from "@mui/material/Chip";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";

import { useFamily } from "@/lib/family-context";

/** The "Family: <name>" chip in the app bar (super only); `setFamilyId` scopes every `/family/*`
 * page and `/api/family/*` call. */
export default function FamilySwitcher() {
  const { family, families, setFamilyId } = useFamily();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  if (families.length === 0) return null;

  return (
    <>
      <Chip
        color="secondary"
        label={`Family: ${family?.name ?? "choose…"} ▾`}
        onClick={(e) => setAnchor(e.currentTarget)}
        sx={{ cursor: "pointer", maxWidth: { xs: 150, md: 260 } }}
      />
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {families.map((f) => (
          <MenuItem
            key={f.id}
            selected={f.id === family?.id}
            onClick={() => {
              setFamilyId(f.id);
              setAnchor(null);
            }}
          >
            {f.name}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}
