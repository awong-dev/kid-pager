# Relay-side SMS through Twilio: evaluation

## Question

Can the relay carry SMS for pager users through a telephone number that belongs to each user,
using a commercial SMS provider, instead of relying only on the pager's own modem?

## What was built

- **One number per person.** A person held at most one SMS number, and a number belonged to one
  person. The number was set by a family admin or the super admin. Sending from a member used
  that member's number, so recipients saw a stable sender.
- **Outbound.** A text to an external SMS contact was a relay peer only when its sender had a
  number. The relay sent the text from the sender's number through the provider's messages API,
  and the delivery state was tracked as queued, sent or failed, with bounded retries for
  transient errors.
- **Inbound.** One webhook received every text. The sender's number was matched against the
  family's known contacts. A text from a known, approved contact was delivered to the pager.
  Any other text was held. The provider's request signature was checked against the public
  webhook URL before anything was stored.
- **Held until approved.** A held text was stored with its body and an open alert was raised for
  the family's admins, with the text contents in the push notification. Approving the alert
  created the contact and the permission edge, then delivered the held texts in order. Blocking
  or dismissing the alert ended the hold. Each unknown number was capped at 25 held texts.
- **Consent.** The relay answered the opt-out and opt-in keywords itself, sent a help reply, and
  sent a welcome message on first opt-in. Outbound messages went only to numbers that had opted
  in. Each relayed message named the sender and the operator, and the first message per number
  each day carried the opt-out instructions. Links and phone numbers in relayed text were
  defanged.
- **Multi-account design (not built).** Because a single personal registration covers one
  number and one campaign, the design proposed one provider account per registration. It had a
  registry of accounts, a per-account token in the secret store, signature checks per account,
  and a rule that a number could only receive from its own account. It was parked before any
  of it was implemented.

## Costs

- **Money.** Each number carried a monthly rent, and each segment was billed per message. The
  design estimate was about 1.2 cents per segment including all fees. Costs grew with the number
  of users and with texts that split into several segments.
- **Registration.** US carrier registration of a brand and a campaign was required for each
  number. The individual (sole proprietor) path allowed one number and one campaign per
  registration, with a daily segment limit per carrier. The business path needed a registration
  number and took days to months to clear. Numbers and registrations could not move between
  primary provider accounts, so a later change of account meant buying new numbers and
  repointing configuration.
- **Consent and content obligations.** Because the relay, not the provider, handled opt-out, the
  relay had to keep its own consent records, answer the keywords, identify the operator in each
  message, and send the disclosure. Message bodies were kept in the relay's store and also in the
  provider's logs.
- **Operations.** Number purchase, campaign attachment and webhook configuration were manual
  console steps. The webhook URL had to match the signed URL byte for byte. Secrets for the
  provider account lived in the secret store, and a change of token needed a redeploy or a
  cache invalidation.
- **Privacy.** A text from an unknown number was relayed in full to the family's phones. The
  sender's identity was the phone number alone, and was used to route the text.

## Failure modes seen in design

- A sender without a number could not send; the pager received a "not set up" reply.
- An unregistered or opted-out recipient failed at the provider, and the failure was recorded
  but not retried.
- A number assigned to two people, or a number reassigned without clearing the old holder, was
  rejected until cleared.
- A flood from one unknown number filled its cap and the rest were dropped and logged.

## Outcome

Not adopted, and the code was removed. The reasons were:

1. The cost and registration overhead were out of proportion to a low-volume family service.
2. Consent and content duties moved onto the relay.
3. Routing by sender phone number needed an approval step for every unknown number.
4. Multi-account operation would have multiplied the registration, token and alert work for
   each new account.

## Replacement

SMS now goes through a **bridge phone**: an Android phone that the family controls, running the
relay's bridge app, that sends and receives texts on its own SIM or a voice-only number. The
relay routes a text to the bridge phone and the phone sends it, and inbound texts from the
phone are routed back to the sender's alias. This needs no provider registration and no
per-message provider fee; the text is charged to the phone's own plan. See
`docs/BRIDGE_PHONE_DESIGN.md`.

Device-direct SMS through the pager's own modem (`docs/PROTOCOL.md` §3.6) remains a separate
path. It works only when the pager's SIM carries SMS.

The contact types a book can carry for SMS (`t:"sms"`, §3.2 of `docs/PROTOCOL.md`) and the
held-until-approved model for unknown senders are carried over to the bridge phone. Their
wire format did not change.
