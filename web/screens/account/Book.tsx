"use client";

/** `/account/book`: the signed-in person's own address book, with their nicknames. A family admin
 * sees any member's book under Family -> Permissions ("On their pager"), not here. */

import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import MemberAddressBook from "@/components/MemberAddressBook";
import { useAuth } from "@/lib/auth-context";

export default function BookPage() {
  const { me } = useAuth();
  return (
    <Stack spacing={2} sx={{ maxWidth: 640 }}>
      <Typography variant="h5" component="h1">
        My address book
      </Typography>
      <Typography variant="body2" color="text.secondary">
        The people and numbers on your pager, and your nicknames for them. A family admin decides who is on it.
      </Typography>
      {me && <MemberAddressBook ownerUid={me.uid} />}
    </Stack>
  );
}
