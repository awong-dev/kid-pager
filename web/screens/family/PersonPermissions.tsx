"use client";

/** One person's permissions, all in one place: messaging rules, approved people and contacts, their
 * address book, group chats, and who can see where they are. Each editor keeps its own Save and the
 * parent keys this component by uid, so one person's unsaved edits never carry over to the next. */

import { useState } from "react";
import Chip from "@mui/material/Chip";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import ApprovedEditor from "@/components/ApprovedEditor";
import MemberAddressBook from "@/components/MemberAddressBook";
import PolicyPicker from "@/components/PolicyPicker";
import SectionBlock from "@/components/SectionBlock";
import { Link } from "@/lib/router";
import PersonGroupChats from "./PersonGroupChats";
import { permissionsHref } from "./permissionsModel";
import type { MemberRow } from "./useFamilyMembers";

export default function PersonPermissions({
  member,
  viewers,
  viewersLoading,
  onApprovedSaved,
}: {
  member: MemberRow;
  /** Aliases allowed to see where this person is (from everyone's approved lists). */
  viewers: readonly string[];
  viewersLoading: boolean;
  /** Called after either editor saved, so the overview re-reads this person. */
  onApprovedSaved: () => void;
}) {
  // The *saved* outbound policy (not the unsaved radio) so the approved list reads as the relay applies it.
  const [savedOut, setSavedOut] = useState<string | null>(null);
  const policyOut = savedOut ?? member.policy.out;
  const name = member.displayName;

  return (
    <Stack spacing={4}>
      <Typography variant="h5" component="h2">
        {name}{" "}
        <Typography component="span" variant="body1" color="text.secondary">
          @{member.alias}
        </Typography>
      </Typography>

      <SectionBlock
        id="perm-rules"
        title="Messaging rules"
        description={`Who ${name} can message, and who can message ${name}. Each is a separate choice.`}
      >
        <PolicyPicker
          uid={member.uid}
          policy={member.policy}
          onSaved={(p) => {
            setSavedOut(p.out);
            onApprovedSaved();
          }}
        />
      </SectionBlock>

      <SectionBlock
        id="perm-approved"
        title="Approved people and contacts"
        description={`The people and outside contacts ${name} may message when the rule above asks for approval. Family members already count. Turn on Locate to let ${name} see where a person is.`}
      >
        <ApprovedEditor
          uid={member.uid}
          familyId={member.familyId}
          policyOut={policyOut}
          onSaved={onApprovedSaved}
        />
      </SectionBlock>

      <SectionBlock
        id="perm-book"
        title="On their pager"
        description={`${name}'s address book: who is on their pager, why each entry is there, their nicknames, and entries they added themselves.`}
      >
        <MemberAddressBook ownerUid={member.uid} />
      </SectionBlock>

      <PersonGroupChats uid={member.uid} name={name} />

      <SectionBlock
        id="perm-location"
        title="Who can see their location"
        description={`Family admins can always locate ${name}. These people can too, because their approved list has Locate turned on for @${member.alias}.`}
      >
        {viewersLoading ? (
          <Skeleton variant="rounded" height={32} />
        ) : viewers.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No one else yet. To allow someone, open them and turn on Locate next to @{member.alias} in their approved
            list.
          </Typography>
        ) : (
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
            {viewers.map((a) => (
              <Chip key={a} component={Link} href={permissionsHref(a)} clickable label={`@${a}`} />
            ))}
          </Stack>
        )}
      </SectionBlock>
    </Stack>
  );
}
