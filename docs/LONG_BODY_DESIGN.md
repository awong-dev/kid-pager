# Long messages: the 160-character cap is removed

The pager accepts and sends message bodies of up to 1024 UTF-8 bytes in place of the old 160
code point cap, and the relay splits anything longer into consecutive messages.

## Constraint that sets the number
The modem library receives one MQTT message into a 1540-byte buffer and drops the rest
(`GOTCHAS.md`, "larger than about 1.5 kB cannot be received"); raising it costs 8x the increase in
internal RAM. So "no limit" is built as: **one message carries up to 1024 UTF-8 bytes, and the
relay splits anything longer into consecutive messages.** 1024 bytes is 1024 Latin characters or
about 340 Chinese characters.

## Decisions
1. **Body cap: 1024 UTF-8 bytes, both directions, no code-point cap.** One constant on each side
   (`MSG_BODY_MAX_BYTES` / `wire.BODY_MAX_UTF8_BYTES`). `PROTOCOL.md` §3.1, §3.3, §4.2, §9.4, §9.5.
2. **Envelope limit 640 -> 1280 bytes** for `pager/{id}/…` (`PAGER_MAX_PAYLOAD`, the relay's 640
   assertions).
3. **Capability key.** Online `/status` gains `mb` (max body bytes, CBOR key: the next free one
   after the receipt keys 74/75, never 52), sent by firmware of this generation as `1024`. It is
   the single switch for everything new in this release: a device that reports `mb` gets long
   bodies, `rcpt` receipts and book type `fed`; a device that does not is treated as before
   (160 code points / 320 bytes, `system` row receipts). Stored as `devices.status.mb`.
4. **The relay splits per recipient.** In `Routing`, before `_create_and_deliver`: recipient cap =
   min over the recipient's non-revoked pagers of (`status.mb` or legacy 320 B/160 cp), or 1024
   when they have no pager. A body over the cap becomes N messages, split on code-point
   boundaries, preferring the last whitespace in the final 20 % of a part, no "(1/3)" prefix,
   consecutive `seq`; parts keep the original `ts` and order by `seq`.
   Wire ids: part 0 keeps the wire id, part i>0 uses `<wire>~<i>`; group copies use the minimum
   cap over members so every member gets the same parts (`groupMsgId~<i>`). The sender's own
   thread shows the parts, which is the truth.
5. **Nothing is refused for length below a sanity bound of 8192 bytes per inbound text** (bridge
   event `text` max 8192 code points; web composer and `/up` stay at one message, 1024 bytes). The
   "Message too long" SMS hint, the `too_long` held-row status and the web's 160/320 counter go away;
   held rows already marked `too_long` are delivered on the next approve.
6. **Outbound SMS.** A page up to 1024 bytes to an SMS contact goes to the phone as one outbox
   item; Android already sends multipart. Its part count feeds the balance (`BRIDGE_HEALTH_DESIGN.md` §13).
7. **Device-direct SMS is unchanged** (single part, 160 GSM-7 / 70 UCS-2, `sms_log` ≤ 160): the
   modem is in text mode and concatenation needs PDU mode. The composer keeps its existing
   too-long refusal only when the peer is on the pager's direct-SMS list.
8. **Firmware storage: bodies move to a PSRAM pool.** `msg_t` keeps the header and a slot index;
   `heap_caps_calloc(32, 1025, MALLOC_CAP_SPIRAM)` in `msg_init` holds the 32 thread bodies
   (fallback without PSRAM: 4 internal slots, older rows show "(text not kept)").
   `msg_body_copy()` copies under the lock. `scr_chat.c` and `scr_home.c` keep headers and fetch
   one body at a time into one static scratch. Stack copies in the ingest/publish path become
   static scratch (the modem event task has 8 KB). Net internal RAM goes down ~40 KB.
9. **Firmware UI.** Composer accepts up to 1024 bytes; the counter appears from 900 bytes as
   `n/1024`; toast "message full". Chat: `CHAT_MAX_ROWS_PER_MSG` 64 -> 96, page step = visible
   rows - 1.
10. **msghist / msgq.** `MSGHIST_REC_MAX` ~1140 (format unchanged, length is already u16). NVS
    `msgq` holds up to 3 bodies. Check `nvs_get_stats` on the bench; under 300 free entries, move
    `msgq` bodies into the msghist partition.
11. **PUBACK before "sent".** A reply is marked SENT, and its stored copy erased, when the modem
    answers `OK` to `AT+SQNSMQTTPUBLISH`, which precedes the broker's PUBACK
    (`+SQNSMQTTONPUBLISH:0,<mid>,<rc>`). The fix is a library patch capturing `<mid>` from
    `+SQNSMQTTPUBLISH: <mid>`; `net_publish_raw(..., uint16_t *mid)`; the PUBLISHED handler calls
    `msg_on_publish_done(mid, ok)` (flags only); `msg_pump` keeps the slot and the msgq body until
    the matching URC with rc 0, and treats rc != 0, a disconnect edge or 30 s of silence as one
    failed attempt (3 attempts, then the 2 h retry). Replies first; acks unchanged.
12. **Order of deployment.** Relay first (it accepts `mb`, splits for old devices), then firmware.

## Implementation notes

- **Constants.** `MAX_ENVELOPE_BYTES = 1280` and `BODY_MAX_UTF8_BYTES = 1024` (`relay/app/wire.py`).
  The names `MSG_BODY_MAX_BYTES` and `PAGER_MAX_PAYLOAD` are not used.
- **Envelope 640 kept for some builds (decision 2).** `LEGACY_MAX_ENVELOPE_BYTES = 640` remains the
  limit for cfg, book and nudge builds (`relay/app/devcfg.py`), because a device that has not
  reported `mb` must still be able to receive them. Decision 2 applies 1280 to every other
  `pager/{id}/…` message.
- **`mb` (decision 3).** CBOR key 76 (`relay/app/wirecbor.py`), JSON name `mb`. The relay accepts
  1 to 8192 (`MB_MAX` in `wire.py`), not only 1024.
- **Split (decision 4).** `relay/app/bodysplit.py` cuts on the last whitespace in the final 20 % of a
  part. Caps are per recipient (`_recipient_cap` in `relay/app/routing.py`): 320 bytes and 160 code
  points without `mb`, 1024 with no pager. Group copies use the smallest cap. Part ids are
  `<wire>~<i>` (`_part_id`).
- **Length refusals (decision 5).** The web composer and `/up` keep the 1024-byte refusal
  (`relay/app/routers/conversations.py`). The bridge text limit is 8192 (`relay/app/routers/bridge.py`).
  SMS inbound has no length refusal.
- **Held SMS (decision 5): deviation.** `relay/app/routers/family.py` still writes status `too_long`
  when the body is empty after cleanup. Decision 5 removes that status. Held rows already marked
  `too_long` are sent on approve.
- **Hook `replace` size.** 8192 bytes, not 1024 (see `docs/HOOKS_DESIGN.md`, Implementation).
- **Firmware.** 96 chat rows per message (`CHAT_MAX_ROWS_PER_MSG` in `firmware/main/scr_chat.c`).
  The body pool is in PSRAM (`firmware/main/msg.c`, `MALLOC_CAP_SPIRAM`). The publish-done hook
  `msg_on_publish_done` is in `msg.c` (decision 11).
- **Not verified on hardware:** msghist sizing (decision 10), the receive and publish ceilings, the
  stack high-water mark during a 1024-byte ingest and reply, `nvs_get_stats`, an end-to-end 1024-byte
  message up and down, and the case where the link drops between `OK` and the PUBACK. The web
  composer counter is not verified.
