# Soracom bearer: plain MQTT through Beam, chosen by the inserted SIM

Status: built, and verified on the bench end to end (section 6). The evaluation that led here is
`docs/SORACOM_EVAL.md`. This document describes the option that was built: a Soracom Beam MQTT entry
point in front of the existing EMQX broker. The UDP downlink design in the evaluation (its step 3)
is not built.

MQTT from the modem to `beam.soracom.io:1883`; Soracom Beam opens the MQTTS leg to EMQX). The bearer
is a property of the SIM. It is picked by the carrier detection the firmware already does
(`firmware/main/carrier.[ch]`), not by a build.

## 1. Requirement

One firmware image runs on both SIMs. A US Mobile SIM behaves exactly as before. A Soracom SIM
attaches with Soracom's APN and credentials and carries its MQTT session through Beam with no TLS
on the pager. The bench unit is driven over USB and through the production relay.

## 2. Facts the design rests on

- The MQTT host and port come from the setup bundle and live in NVS `ident` (`host`, `port`);
  `xport_lte.cpp` connects to them. `net.cpp` `configure_session()` always builds TLS profile 2
  naming CA slot 12 and calls `mqttConfig(dev_id, dev_id, mqtt_pw, 2)`. The vendored library only
  appends the TLS profile to `AT+SQNSMQTTCFG` when the id is non-zero (`WalterMQTT.cpp`), so
  `tls_profile_id = 0` is the library's "no TLS" form.
- `carrier.c` detects the SIM from the IMSI's first six digits plus EF_GID1 and persists a fixed
  choice in NVS namespace `carrier` (`mode`, `apn`). `effective_apn()` (`net.cpp`) resolves: typed
  `;apn=` → fixed choice → auto-detect → bundle `apn` → blank. The greeting screen in Setup mode
  already runs `net_check_sim()` (AT+CIMI at NO_RF).
- The library has `setPDPAuthParams()`, which the firmware does not use (see 3.2).
- The production broker is EMQX Cloud Serverless, `broker.example.com:8883`, with a certificate chained
  to DigiCert Global Root G2. Beam validates with a normal root store, so it accepts it with no extra
  configuration.
- Devices authenticate to EMQX with a per-device username and password (username = device id;
  `relay/app/emqx_admin.py`). Beam's MQTT entry point (version 201912) has a credential
  **pass-through** mode (`useClientCredentials`): the pager's own username and password go through to
  session assumption still holds.
- The relay sees only `topic`, `payload` and `qos` from the webhook, and authenticates the device by
  the HMAC in the envelope (`ingest.py`). A Beam-relayed session that publishes the same topics is
  indistinguishable from a direct one. **The data path needs no relay change.**
- Soracom's APN for Air for Cellular is `soracom.io`, user `sora`, password `sora` (PAP or CHAP).
  The IMSI prefix for Soracom Global SIMs is read from the SIM on the bench (section 6, step A), and
  the preset is written from that reading.
- A device-originated SMS on a Soracom SIM can only reach Soracom's own endpoint, never a phone. On

## 3. Firmware design

### 3.1 Carrier presets carry a bearer and PDP credentials

`carrier_preset_t` has `bearer` (`CARRIER_BEARER_DIRECT` | `CARRIER_BEARER_BEAM`), `auth_proto`
(none/PAP/CHAP), `auth_user`, `auth_pass`, and `sms_mo` (bool, may the pager send SMS through the
modem). The Soracom preset:

```
{ "Soracom", "soracom.io", "<PLMN prefixes read on the bench, e.g. 29505x>", NULL,
  CARRIER_BEARER_BEAM, PAP, "sora", "sora", sms_mo=false }
```

Existing presets: bearer `direct`, auth none, `sms_mo=true`. "Automatic" resolves to whatever the
SIM detects. "Carrier default" and a typed custom APN are `direct` with no auth, exactly as before.

A pure function `carrier_effective(const char *imsi, const char *gid1, const char *typed_apn)`
returns the preset in force (fixed choice, else detection, else NULL). It is the single place
`net.cpp` asks "which carrier?". `effective_apn()` keeps its precedence but reads the APN, auth and
bearer from the same answer, so they cannot disagree.

A debug and Setup console command `bearer [auto|direct|beam]` stores an override in NVS `carrier`
(`bearer`; absent = auto), for A/B on one flash. Experimental behaviour goes behind a runtime flag.
`direct` on a Soracom SIM means TLS straight to EMQX over Soracom data. The override can force
`direct` on any SIM but `beam` only when the carrier in force is a Beam preset. `beam.soracom.io`
resolves into carrier-grade NAT address space, so on another operator's network a forced `beam` would
send the MQTT password in clear text to whatever host answers there. The override is read once at
boot; there is no live bearer switch. `net_bringup()` and `net_bootstrap_attach()` resolve the carrier
once and cache the bearer in RAM. `configure_session()`, which runs after every modem reset, reads the
cache and never re-reads the SIM.

### 3.2 PDP context authentication

The library's `setPDPAuthParams()` never emits anything, so the firmware does not call it. It returns
OK early when the context's stored `auth_proto` is NONE, and nothing writes that field, so `AT+CGAUTH`
is unreachable through it.

Instead, right after `definePDPContext(PAGER_PDP_CTX_ID, apn)`, and while the modem is still at
CFUN=4 and before `setOpState(FULL)` (the LTE attach sets up the default bearer, so credentials must
already be in place), the firmware sends the raw command through `WalterModem::sendCmd()`:
`AT+CGAUTH=1,1,"sora","sora"` (1 = PAP, 2 = CHAP). It logs `pdp auth: PAP user=sora` once.

The modem keeps `+CGDCONT` and `+CGAUTH` in its own NVM across resets. A pager moved from a Soracom
SIM back to US Mobile would therefore attach with stale credentials. To keep the direct path
unchanged on a pager that never saw a Soracom SIM, the firmware sets NVS `carrier/auth=1` when it
writes CGAUTH. Only when that flag is set and the carrier in force has no credentials does it send
`AT+CGAUTH=1,0` and clear the flag. A typed `;apn=soracom.io` on a Soracom SIM still takes auth and
bearer from the detected preset, because typing the preset's own APN is not a custom APN.

### 3.3 Session configuration on the Beam bearer

In `configure_session()` and the bootstrap path:

| | direct | beam |
|---|---|---|
| Host, port | NVS `ident` host/port (from the bundle) | `PAGER_BEAM_HOST "beam.soracom.io"`, `PAGER_BEAM_PORT 1883` (compile-time constants) |
| TLS profile | 2, slot 12, VALIDATION_CA or NONE per catrust | **none**: `mqttConfig(dev_id, dev_id, mqtt_pw, 0)`; no `tlsConfigProfile`, no CA slot write |
| Client id / username / password | dev_id / dev_id / mqtt_pw | identical (pass-through) |
| catrust / cafetch | as designed in `V02_DESIGN.md` §4 | skipped. A `ca_url`/`ca_sha` in the bundle is ignored: `ident` has no field for the pointer, so the identity is stored unpinned. `/status.tls` = `proxy` |
| `cfg.ca` push | two-phase apply | refused with a failure ack, nothing fetched. A trial reconnect over the plaintext hop would "validate" the scratch-slot CA without using it, and then commit it |
| Bootstrap (`setup <code>`) | TLS none-validation to `host:port` from the code | plain MQTT to Beam as `boot-<bid>` with the bootstrap password, keepalive 60; same topics. The code's host is logged and ignored, because Beam's group destination decides the broker |

Branch points, each as `if (bearer == BEAM)` with the existing code untouched in the `else`:
`configure_session()` skips the CA and `tlsConfigProfile` block and passes profile 0 to
`mqttConfig`; `xport_lte.cpp` picks host and port; `setup.c` skips `net_tls_profile_bootstrap()`,
passes the Beam host and port, and skips the CA fetch; `net_bootstrap_connect()` passes profile 0;
`catrust_service()` refuses `cfg.ca`. `catrust_before_reconnect()` and `catrust_on_mqtt_tls_fail()`
need no branch: they only touch profile 2, which the Beam session does not name, and a plain-TCP
connect cannot return `WALTER_MODEM_MQTT_TLS`.

Not yet confirmed: that `AT+SQNSMQTTCFG` without `<sp_id>` clears a previously configured profile
without a modem reset. The bench checks it with `AT+SQNSMQTTCFG?`.

The bundle's `host` and `port` stay stored in `ident`, so a later SIM swap back to a direct bearer
works without re-provisioning. On `beam` they are simply not used. The hazard in `GOTCHAS.md` ("a TLS
profile naming no CA sends plaintext") does not apply: the direct path always names slot 12.

### 3.4 Reporting

The encoder is `modes.c` `build_status_cbor()`. `tls` comes from `catrust_state_name()`, which returns
`proxy` on the Beam bearer. Key 70 is free (`PROTOCOL.md` §10 ends at 69; `wirecbor.py` matches). The
relay's `tls` is a `Literal["unpinned","pinned","broken"]`, so the relay change in section 4 must
deploy before any firmware that can send `proxy`.

`/status` has key **70 `car`** (tstr ≤ 24, the preset label, e.g. `Soracom`, `US Mobile Dark Star`;
absent when no preset is in force). `tls` can be **`proxy`** (TLS terminated by Beam, not on the
pager). `xport` stays `lte`. The Device screen's Carrier row shows the label as usual, and the boot
splash's network status line says `via Beam` when the bearer is beam.

### 3.5 Debug console additions

- `mqtttest <host> <port> [ca|noneca|emptyca|plain]`: `plain` configures with profile 0, the
  Beam-shape connection, so the hop can be probed independently of the session code.
- `bearer [auto|direct|beam]` (3.1), and `carrier`, which prints the bearer and auth in force.

### 3.6 Sleep, eDRX and liveness

Modem MO SMS is gated in one place, `net_sms_send()`, which returns false when `sms_mo` is false.
Both callers (`sms.c` send queue and `smstest`) already audit a failure.

Open question: what the visited network grants a roaming LTE-M device. The bench measures
registration time, the granted eDRX (`AT+CEDRXRDP`) and page latency. If eDRX is refused, that is a
finding to report, not something this work fixes.

## 4. Relay and web

The data path is unchanged (section 2). What changes:

- `wire.py` `/status` model: `car: str | None` (key 70), and `tls` accepts `proxy`. Both are stored on
  `devices.status.car` and `status.tls` like the other device-reported fields, and are display only.
- `apn_presets.py` adds `Soracom` → `soracom.io`, so the admin APN picker offers it. The firmware's
  own detection is what sets PAP credentials; the preset keeps the UI accurate.
- Web: the Devices tables and device page show a **Carrier** chip from `status.car`. The CA-trust
  column renders `proxy` as "via Beam", and the SMS panel is hidden or annotated for a device whose
  `tls` is `proxy` (the device cannot originate SMS; relay SMS still works).
- `PROTOCOL.md` §5.1 (`car`, `tls: proxy`), §6.1 (bearer row: Beam plain MQTT, keepalive 1200 inside
- Tools: `tools/pager_client.py` already speaks plain MQTT on 1883 and is the local stand-in for a
  Beam-side client. `tools/soracom_beam.py` (section 5) does the Soracom-side setup.

## 5. Soracom-side configuration

Beam is configured per **Group** in the Soracom console (Global coverage, `g.api.soracom.io`):

1. Create a group, e.g. `pager-beam`.
2. Basic settings → SORACOM Beam → add an **MQTT entry point** (`mqtt://beam.soracom.io:1883`):
   destination `mqtts://broker.example.com:8883`, version **201912**, credentials **pass-through**
   (`useClientCredentials: true`; no username or password of Beam's own, no client certificate),
   enabled.
3. Add the pager's SIM to that group.

`tools/soracom_beam.py` does the same through the API when `SORACOM_AUTH_KEY_ID` and
`SORACOM_AUTH_KEY` (a SAM user auth key) are in the environment:
`auth → list/create group → PUT /v1/groups/{id}/configuration/SoracomBeam → PUT /v1/sims/{simId}/set_group`.
The auth key lives outside the repository.

## 6. Bench verification (evidence summary)

Test sequence: A identity; B attach and provision on the direct bearer; C messaging on the direct
bearer; D release build on the direct bearer; E the Beam hop; F regression (host tests for
`carrier_effective()`, and the direct path on the same image with `bearer direct`).

- **A and B (direct bearer).** `setup <code>` sent `AT+CGAUTH=1,1,"sora","sora"` after `AT+CGDCONT`.
  The unit registered while roaming (`+CEREG: 5`) on an AT&T LTE-M network in about 17 s, the
  bootstrap MQTTS to EMQX was usable 4 s later, the CA fetch took 2.6 s, and `SETUP done` came 26 s
  after the command. The normal session was usable 32 s after boot on the debug build (25 s on
  release). eDRX was granted while roaming: `+CEDRXRDP: 4,"0010","0010","0001"` (20.48 s cycle,
  2.56 s paging window). RSSI was about −69 to −65 dBm. In production the device showed CA trust
  "Server verified" and the Carrier chip "Soracom" (`/status` key 70 end to end).
- **C (messaging, direct).** Two pages from the web arrived 1–6 s after Send, each with `shown`
  published within 1 s. Two console-typed replies reached the web chat within seconds. Console note:
  Enter is `key \\n` on the wire, and bursts are limited to 8 keys (see `docs/HARDWARE_TESTING.md`).
- **D (release, direct).** The release image was flashed over the debug one (app and otadata only).
  The boot banner showed carrier Soracom with PAP, `boot complete, entering sleep mode` at 19.8 s, and
  the session usable at 24.6 s. A one-hour relay-side watch, with no serial polling, saw pages sent at
  +10 and +40 minutes both "on pager" within a minute of Send, battery samples at 3700 mV, 152 sleeps,
  2 reconnects (both deliberate reboots) and no ext1 wakes.
- **E (Beam hop).** `tools/soracom_beam.py` ran live: the group was created, the SoracomBeam entry was
  accepted with the section 5 field names (echoed back unchanged by GET), and the SIM was set to the
  group. On the debug image with `bearer auto` (beam): `bearer beam: no TLS profile, MQTT to
  beam.soracom.io:1883 plain`, `+SQNSMQTTONCONNECT:0,0` 1 s after the connect, and the session usable
  **17.4 s after boot** (direct: 32 s; no TLS handshake, no CA step). The web showed "via Beam" and
  "Soracom", and the CA push and un-pin controls were hidden. A page through Beam arrived about 1 s
  after Send, `shown` followed within 1 s, and a console reply reached the web chat. Re-provisioning
  through Beam worked: the bootstrap ignored the code's host and used plain MQTT to
  `beam.soracom.io:1883`, `SETUP CA fetch skipped: Beam bearer terminates TLS`, and `SETUP done` came
  10 s after the command (direct: 26 s). The release image then booted to sleep with the session usable
  at 16 s, and a page sent to the sleeping unit was "on pager" about 40 s after Send. No refusal from
  Beam and no CME error on the plain connect were seen.

## 7. Decisions and limitations

1. **Pass-through credentials, not IMSI-injected.** This keeps the EMQX users and the relay unchanged.
2. **Not implemented: AEAD page bodies.** They are a protocol change on both ends and are tracked in
   `docs/ROADMAP.md`. Until they exist, the Soracom network and the roaming carrier can read page bodies
   on the Beam bearer. The HMAC still protects integrity.
3. **Beam host and port are compile-time constants.** They are Soracom's fixed entry point, not
   per-deployment data.
4. **Soracom detection is by IMSI prefix**, like the existing presets. If a later Soracom SIM has a
   different prefix, the Carrier menu's `Soracom` choice is the fallback the carrier design already has.

## 8. Enrollment from the admin UI

The admin web UI can move SIMs into the Beam group without the CLI. Three super-only routes in
`relay/app/routers/admin.py`, backed by `relay/app/soracom.py` (which `tools/soracom_beam.py` wraps):
