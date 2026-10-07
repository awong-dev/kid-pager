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
export const OUTBOUND_POLICIES: PolicyOption[] = [
  {
    code: "open",
    label: "Open",
  },
];

// `users.policy.in`: who may reach a member. An unknown number is held and raises an alert;
export const INBOUND_POLICIES: PolicyOption[] = [
  { code: "people", label: "People", description: "Only approved people may reach them." },
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
