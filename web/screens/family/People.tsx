"use client";

/** `/family/people` "People & contacts": every chat target of the family on one page, in three
 * sections (`#members`, `#contacts`, `#groups`). The page subscribes to the members once and passes
 * them down; each section has its own loading and error state. */

import { useCallback, useState } from "react";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import JumpLinks from "@/components/JumpLinks";
import { useScrollToAnchor } from "@/lib/useScrollToAnchor";
import ContactsSection from "./ContactsSection";
import GroupChatsSection from "./GroupChatsSection";
import MembersSection from "./MembersSection";
import { useFamilyMembers } from "./useFamilyMembers";

const SECTIONS = [
  { id: "members", label: "Family members" },
  { id: "contacts", label: "Outside contacts" },
  { id: "groups", label: "Group chats" },
] as const;
// `federation` is the other-families block inside Outside contacts (alerts link to it).
const ANCHORS = [...SECTIONS.map((s) => s.id), "federation"];

export default function PeopleAndContactsPage() {
  const family = useFamilyMembers();
  const [settled, setSettled] = useState({ contacts: false, groups: false });
  const contactsSettled = useCallback(() => setSettled((s) => ({ ...s, contacts: true })), []);
  const groupsSettled = useCallback(() => setSettled((s) => ({ ...s, groups: true })), []);
  // Scroll to a #fragment once every section has content, so the layout has stopped moving.
  useScrollToAnchor(!family.loading && settled.contacts && settled.groups, ANCHORS);

  return (
    <Stack spacing={4}>
      <Stack spacing={1}>
        <Typography variant="h5" component="h1">
          People &amp; contacts
        </Typography>
        <Typography variant="body1" color="text.secondary">
          Everyone your family&apos;s pagers can message.
        </Typography>
        <JumpLinks items={SECTIONS} />
      </Stack>
      <MembersSection {...family} />
      <ContactsSection members={family.members} onSettled={contactsSettled} />
      <GroupChatsSection onSettled={groupsSettled} />
    </Stack>
  );
}
