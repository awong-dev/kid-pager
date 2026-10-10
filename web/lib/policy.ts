/** Conversation policy code, label and description tables (outbound and inbound). */

export interface PolicyOption {
  code: string;
  label: string;
  /** One-line description shown under the radio button. */
  description: string;
  /** Not offered as a new choice; still shown when it is the current value. */
  hidden?: boolean;
}

// `users.policy.out`: who a member can start or continue a chat with. "Numbers" are external
// SMS contacts reached through the member's bridge phone.
export const OUTBOUND_POLICIES: PolicyOption[] = [
  {
    code: "open",
    label: "Open",
    description: "Message any person or any number. Admins are alerted when a new conversation starts.",
  },
  { code: "people", label: "People", description: "Only approved people, no numbers." },
  { code: "people_sms", label: "People + Numbers", description: "Approved people and approved numbers." },
  { code: "sms", label: "Numbers", description: "Only approved numbers, no people." },
  { code: "any_sms", label: "Any number", description: "No people, but any phone number." },
];

// `users.policy.in`: who may reach a member. An unknown number is held and raises an alert;
// approving it needs a policy whose numbers column is not "none".
export const INBOUND_POLICIES: PolicyOption[] = [
  { code: "any", label: "Anyone", description: "Any person or any approved number may reach them." },
  { code: "people", label: "People", description: "Only approved people may reach them." },
  {
    code: "people_sms",
    label: "People + Numbers",
    description: "Approved people and approved numbers may reach them.",
  },
  { code: "sms", label: "Numbers", description: "Only approved numbers may reach them, no people." },
  {
    code: "any_sms",
    label: "Any number",
    description: "No people; approved numbers reach them and unknown numbers are held for your approval.",
  },
];

function labelOf(options: PolicyOption[], code: string): string {
  return options.find((o) => o.code === code)?.label ?? code;
}

export function outboundLabel(code: string): string {
  return labelOf(OUTBOUND_POLICIES, code);
}

export function inboundLabel(code: string): string {
  return labelOf(INBOUND_POLICIES, code);
}
