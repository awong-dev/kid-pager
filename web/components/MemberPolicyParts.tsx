"use client";

/** The two permission parts of the member drawer, each self-contained so the Permissions page can
 * mount them without the drawer:
 *  - `MemberPolicyPart`: who this member may text and be texted by (`PolicyPicker`, which has its
 *    own Save).
 *  - `MemberApprovedPart`: the approved list (`ApprovedEditor`). It needs the member's *saved*
 *    outbound policy (it changes what the list means), so the owner of both parts keeps that
 *    value: `MemberPolicyPart` reports it through `onSaved`, `MemberApprovedPart` takes it. */

import Divider from "@mui/material/Divider";
import Typography from "@mui/material/Typography";

import ApprovedEditor from "@/components/ApprovedEditor";
import PolicyPicker from "@/components/PolicyPicker";
import type { UserDoc } from "@/lib/types";

type PolicyMember = { uid: string; familyId: string | null; policy: UserDoc["policy"] };

export function MemberPolicyPart({
  member,
  onSaved,
}: {
  member: Pick<PolicyMember, "uid" | "policy">;
  onSaved: (policy: UserDoc["policy"]) => void;
}) {
  return (
    <>
      <Divider />
      <Typography variant="subtitle2">Policy</Typography>
      <PolicyPicker uid={member.uid} policy={member.policy} onSaved={onSaved} />
    </>
  );
}

export function MemberApprovedPart({
  member,
  policyOut,
}: {
  member: Pick<PolicyMember, "uid" | "familyId">;
  policyOut: UserDoc["policy"]["out"];
}) {
  return (
    <>
      <Divider />
      <Typography variant="subtitle2">Approved</Typography>
      <ApprovedEditor uid={member.uid} familyId={member.familyId} policyOut={policyOut} />
    </>
  );
}
