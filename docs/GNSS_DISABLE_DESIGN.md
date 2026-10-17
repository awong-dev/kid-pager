# Device config: disable GNSS (`cfg.loc.gnss`)

A per-device setting, pushed from the web app, that stops the pager from ever powering the GNSS
receiver. The motivating case is a module whose GNSS antenna connection is damaged. Location keeps
working from the serving cell.

## Decisions

| | Decision |
|---|---|
| D1 Wire | A sixth `cfg` sub-map, `loc` (CFG key **5**), map `{gnss}`; `gnss` is CBOR key **0**, bool. JSON `"cfg":{"loc":{"gnss":false}}`. `gnss` absent = no change. Unknown sub-keys ignored. Applied and acked `shown` immediately, like `lock`. Newest-only and re-published on an online edge like every `cfg`. |
| D2 Persistence | Device: NVS namespace `loc`, key `gnss` (u8); absent = 1 (enabled). RAM-cached, loaded in `loc_init()`. Relay: `gnss_enabled: bool` on the device document (default `True`, not a secret, admin-SDK writes only, no rules change). |
| D3 Effect | When disabled, `loc.c` never calls any `net_gnss_*()` function. Every attempt (on-demand `loc_req`, `gnsstest`, scheduled while-moving) short-circuits at the **top** of `LOC_PH_ASSIST_CHECK`, before `net_gnss_assistance_due()`, with the existing "any GNSS failure sends a cell report and stops" path (`finish_attempt(false)`). The scheduled GNSS-while-moving timer never fires while disabled (`loc_track_gnss_due()` is not consulted, same as `move_gnss_s == 0`), so no attempt is even started for it. `loc_move_s` in `/status` is left as configured. |
| D4 Status | `/status` gains `gnss` (CBOR key **69**, uint `0`/`1`; JSON `gnss`), always present once this firmware runs. Absent = firmware predating the field. Display only. |
| D5 Relay API | `GET`/`PUT /api/devices/{id}/gnss`, body/response `{en: bool, pending: bool, reported: int|null}` (`reported` = last `/status` `gnss`). Owner or admin, the `_require_owner_or_admin` gate. `PUT` stores `gnss_enabled`, then `devcfg.push_loc(device_id, gnss=en, broker=…)` under its own pending slot `pendingCfgLoc`. |
| D6 Web | A `GnssPanel` on the device page: one switch "GPS (GNSS)", a pending chip while unacked, and the device's last reported value. |
| D7 Console | Debug build: `gnss [on|off]` prints or persists the flag (NVS), same style as `loctrack`. |

## Behaviour notes

- Relay: a `/status` `gnss` value outside `0|1` is dropped to `None` (as `bpull` is), not rejected as
  a malformed envelope. `app/ingest.py` passes `gnss` through to `update_status`, so `reported` is
  populated.
- A device that never received `cfg.loc` behaves as `gnss: true`.
- The flag survives reboots (NVS `loc/gnss`) and is reported in every `/status`.
- Firmware `/status` writer: the declared map pair count includes the new field; the status buffer
  still has room for it.

## Verification

- Host tests: `firmware/host/test_cfg.c` (a `cfg` carrying `loc=5` sets `have_loc` with the right
  span and still parses alongside the other sub-maps); `firmware/host/test_loc.c`
  (`loc_parse_cfg_submap` accepts `{0:false}`, `{0:true}` and `{}`, ignores unknown keys, rejects a
  non-map).
- Relay: encode/decode round trip for `cfg.loc` and status `gnss`; `PUT` stores the flag, publishes
  one `/down cfg` with `{"loc":{"gnss":false}}`, and `pending` flips true then false after the
  `shown` ack (`relay/tests/test_gnss.py`, `relay/tests/test_devcfg.py`).
- Web: the web app's lint, type check and build.
- Limitation: not bench-tested on hardware.
