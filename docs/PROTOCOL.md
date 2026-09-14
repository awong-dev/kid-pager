


**Conventions used below**
- `NEEDS HUMAN DECISION` = would require a paid service, a firmware dependency beyond
  `walter-modem` / `esp_timer` / a display driver, or a GPIO change. Not decided here. Collected in §12.
- `Not verified` = a hardware or vendor-library fact this document assumes but cannot confirm; each
  one names the cheapest experiment that settles it.
- Power numbers are labelled `(estimate)`, `(vendor)` or `(TBD — hardware measurement)`. `(vendor)`
  means a figure documented by DPTechnics in the `walter-modem` source; everything else is an
  references. Those references are against that version; re-check them if the component is upgraded.
  v1.5.0 rewrote the MQTT API (event-driven receive, `mqttReceive()`, `mqttDidRing()` deprecated) — do
  not read conclusions drawn against older releases into this document.
---

## 1. Identifiers

| Thing | Format | Max len | Notes |
|---|---|---|---|
| Message id (device-originated, up) | `u_` + 8 lowercase hex | 10 | from `esp_random()` |
| Session id | `s_` + 8 lowercase hex | 10 | per **cold boot**, not per deep-sleep wake; lives in RTC memory (§9) |

`id` is opaque to every consumer. Validators accept `^[a-z0-9_]{3,16}$` so a future generator can
change the prefix scheme without a protocol version bump.

---

## 2. Topics


| Topic | Direction | QoS | Retained | Publisher | Subscriber |
|---|---|---|---|---|---|
| `pager/{device_id}/down` | relay → device | 1 | **false** (fixed) | relay | device only (`pager/{own_id}/down`) |


---




```json
{"v":1,"id":"m_7f3a","ts":1757700000,"from":"parent","body":"Pickup at 3:15 by the gym","ack":null}
```

### 3.1 Fields

| Field | Type | Required | Range / max | Meaning |
|---|---|---|---|---|
| `id` | string | **yes** | `^[a-z0-9_]{3,16}$` | Message id (§1). On an ack, this is the **down message's** id. |
| `ts` | int | **yes** | 0 or 1×10⁹…2×10⁹ | Unix epoch **seconds, UTC**. Set by the publisher. |
| `ack` | string \| null | yes; `null` on content messages | `shown` \| `read` | Ack state being reported. |

Additional rules:
- `body` MUST NOT contain Unicode control characters `U+0000`–`U+001F` or `U+007F`. Relay strips
  JSON escaping bounded, and the e-paper renderer has no control-char handling.)*
- `body` MUST NOT be empty on a content message.
- **Unknown fields MUST be ignored, not rejected.** This is the forward-compatibility rule that
  makes §11 additive.
- Field order is unspecified; a receiver MUST NOT depend on it. Publishers SHOULD emit

### 3.2 Message kinds


A receiver distinguishes an ack from a content message by `ack !== null`. A payload with both a
non-null `ack` and a non-empty `body` is **malformed** (§3.4).

### 3.3 Envelope size limit


```
```


### 3.4 Malformed payload handling


- **Device:** log, increment a counter, **do not ack**, do not render, do not reboot. The message
  stays `sent` at the relay and the parent UI shows it as undelivered.
- The relay's HTTP API rejects oversize/invalid bodies with `400` **before** anything is stored, so
  a malformed message never reaches the air interface.

### 3.5 Clock

The device sets `ts` from the LTE network clock obtained at attach (NITZ / modem RTC via
`walter-modem`). If no network time is available yet, the device publishes `ts: 0` and the relay
purely for timestamps; the relay is already authoritative for thread ordering.)*
Thread order in the parent UI is the relay's insertion order (SQLite rowid), **not** `ts`.

---

## 4. Ack state machine (down messages)

**The relay is authoritative.** The device is a reporter of events; it never holds the canonical
state. States are **monotonic** — a message never moves backwards.

```
 (HTTP POST) ------------> queued --------------> sent --------------> shown -------------> read
```

| State | Owner | Entered when | Notes |
|---|---|---|---|

### 4.1 Rules

1. **Idempotent.** A repeated ack for a state already reached is a no-op (log at debug). QoS 1 makes
2. **Out of order.** A `read` arriving without a prior `shown` promotes the message to `read` and
3. **Unknown id.** An ack for an id the relay has never stored is logged and dropped. It MUST NOT
4. **Wrong device.** An ack arriving on `pager/A/up` for a message addressed to device `B` is
5. **No timeout state change.** Between `sent` and `shown` there is no automatic transition other
6. **Device-side ack durability.** An ack that could not be published (no session, PUBACK missing)
7. **Device-side dedup.** The device keeps the last **16** received message ids in RTC memory,

### 4.2 Up messages (student replies)

**Up messages have no relay-side lifecycle beyond storage, and the relay sends no ack back to the
radio wake per reply for information the device already has from its own PUBACK; the parent page is

Device-side only, in RTC memory: a **2-entry** queue holding the reply body at full fidelity

```
```


---

## 5. Status / LWT contract

Topic `pager/{device_id}/status`, **QoS 1, retained = true**, both for device publishes and for the
broker-generated LWT.

### 5.1 Online payload (117 bytes for this example)

```json
{"v":1,"state":"online","mode":"sleep","batt_mv":3280,"rssi":-95,"session":"s_3ab91c02","ts":1757700000,"fw":"0.1.0"}
```

| Field | Type | Required | Range | Meaning |
|---|---|---|---|---|
| `v` | int | no (default 1) | `1` | Schema version |
| `state` | string | **yes** | `online` \| `offline` | See §5.2 for the precise meaning |
| `session` | string | **yes** | `^s_[0-9a-f]{8}$` | Cold-boot session id (§1). Lets the relay tell a reboot from a deep-sleep cycle. |
| `ts` | int | yes when `online` | epoch s, or 0 | Same rule as §3.5 |
| `fw` | string | no | ≤16 chars | Firmware version |

### 5.2 LWT payload (48 bytes)

Registered in CONNECT, so it cannot contain live data:

```json
{"v":1,"state":"offline","session":"s_3ab91c02"}
```

**`state` means "the MQTT session is alive", not "the ESP32 is awake."** In sleep mode the modem
keeps the TLS+MQTT session up on eDRX while the ESP32 is in deep sleep; that device is `online` with
`mode:"sleep"`. This distinction is load-bearing for the UI copy — do not render `online` as
"awake".

### 5.3 Relay behaviour on status

- On a retained `offline` or an LWT `offline`: mark the device offline; the parent UI shows
  "last seen <time>". No message state changes (§4.1 rule 5).
  - Order: oldest first.
- The relay MUST NOT publish anything to `/down` on a timer for liveness. There is no application

### 5.4 Device publish cadence

Status is published: (a) immediately after MQTT connect, (b) on every mode change, (c) when
`batt_mv` has moved more than 50 mV since the last publish, and (d) as a heartbeat, at most
**once per 3600 s**.

≈ 0.33 kB of data and ≈ 0 extra radio sessions. An independent hourly status timer would add ~24
`now - last_status_epoch >= 3600`.)*

Status is **never** published on a plain paging wake or on receipt of a down message.

---

## 6. Session, keepalive and eDRX

### 6.1 Session

| Parameter | Value | Rationale |
|---|---|---|
| LWT | **NOT SETTABLE from the library** | `mqttConfig()` emits `AT+SQNSMQTTCFG=0,"<clientId>"[,"<user>","<pass>"][,<tlsProfileId>]` and stops there (`src/proto/WalterMQTT.cpp:53-75`) — no will topic, message, QoS or retain argument, and grepping the whole of `src/` for `will`/`lastwill` returns nothing. §5.2's LWT contract therefore has no implementation path through the typed API. Fallback: `WalterModem::sendCmd()` (public) can queue a raw `AT+SQNSMQTTCFG=...` carrying the will parameters *before* `mqttConnect()`. Not verified against the Sequans AT manual. If that fails, the relay must fall back to inferring offline from keepalive expiry and §5.2's LWT becomes advisory. Tracked in §12. |

### 6.2 Keepalive

| Parameter | Value |
|---|---|

### 6.3 eDRX (sleep mode)

| Parameter | Value | Note |
|---|---|---|
| Carrier acceptance | The network may grant different eDRX/PTW values than requested. Firmware MUST log the **granted** values and use them for its latency math. | Not verified per SIM or carrier. **v1.5.0 provides an API for it** — no log-scraping needed. Register `setNetworkEventHandler()` (`src/WalterModem.h:5924`); on `WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED` (`:1348`) the handler receives `WMNetworkEventData.edrx { actType, requestedEdrx[16], nwProvidedEdrx[16], pagingTimeWindow[16] }` (`:2388-2405`), parsed by the library at `src/WalterModem.cpp:1914-1930`. **`nwProvidedEdrx` is the granted value and is the one §6.5's latency math must use.** The same handler's `cereg` branch also carries granted PSM values (`activeTime`, `periodicTau`, `hasPsmInfo`), which is how firmware confirms PSM really is off. Use `configEDRX(WALTER_MODEM_EDRX_ENABLE_WITH_RESULT, …)` (mode `2`) so the modem emits them. |

### 6.4 How these interact with "one persistent session"

The MQTT session lives **inside the modem** and survives ESP32 deep sleep. Therefore:

3. The ESP32 does **not** wake for the keepalive (§6.2). The only periodic ESP32 wake in sleep mode

### 6.5 Latency budget check


| Term | Sleep mode (target ~30 s) | Active mode (target < 5 s) |
|---|---|---|
| network → modem: paging + RRC setup | ≤ 20.48 s (eDRX cycle) + 1–2 s, but the modem does this **while the ESP32 sleeps**, so only the eDRX term is serial: **≤ 20.48 s** | C-DRX, already RRC-connected: **≤ 0.5 s** |
| modem → ESP32: URC held off by RTS until the next wake | **≤ T = 5.0 s** | **≤ T = 2.0 s** |
| light-sleep wake + URC flush + event dispatch (~30 ms, §8.0) + `mqttReceive()` round trip | **0.3 s** | 0.3 s |
| JSON parse + partial e-paper refresh | 0.5–1.5 s | 0.5–1.5 s |
| **worst case** | **20.48 + 5.0 + 0.3 + 1.5 = 27.3 s — fits 30 s with 2.7 s margin** | **0.5 + 2.0 + 0.3 + 1.5 = 4.3 s — fits 5 s with 0.7 s margin** |

Both margins are thin, and both are sensitive to the wake interval `T`, which §8.2 chose for
energy reasons. Two things break this budget, and firmware MUST log enough to detect either:

1. A granted eDRX cycle larger than the requested 20.48 s (§6.3). At the next standard value,

---

## 7. Data budget

### 7.1 Per-frame overhead assumptions

- MQTT PUBLISH QoS 1 = 2 B fixed header + 2 B topic length + topic + 2 B packet id + payload.
- MQTT PUBACK = 4 B. PINGREQ/PINGRESP = 2 B each.
- TLS 1.2 AES-128-GCM record = **+29 B** (5 header + 8 explicit nonce + 16 tag). TLS 1.3 would be
  +22 B; using the worse number.
- IPv4 + TCP headers = **+40 B** per segment. Bare TCP ACKs counted at 40 B where not piggybacked.

### 7.2 Per-exchange totals

|---|---|---|---|---|

### 7.3 Monthly projection


```
```


```
```


real measurement to sanity-check it. **This does not replace §7.3** — it validates the
application-layer MQTT byte counts that §7.3's per-exchange table (§7.2) is built from; it does not,
and cannot, measure the TLS or LTE-M cellular framing §7.3 assumes for the real device↔broker link,
because there is no TLS and no cellular hop in this setup.

**What was measured.** `relay/docker-compose.yml`'s real `eclipse-mosquitto` broker and the real
relay container (no fake transport, no unit-test mocks), with one simulated device
bridge network. Traffic sent, matching the "measurable, non-modem" slice of §7.3's nominal profile
relay's own keepalive is a plain 60 s service keepalive per `RELAY_KEEPALIVE_S`
excluded from this measurement, not estimated).

**How.** `docker stats --no-stream --format '{{.NetIO}}' relay-broker-1` before and after the run,
taken once the device's CONNECT/SUBSCRIBE/initial-status handshake had settled (2 s idle), so the
delta is steady-state per-message traffic, not connection setup. The broker container serves no
HTTP, so 100% of its NetIO is MQTT protocol bytes — this sidesteps having to separate the relay's
HTTP (parent API) traffic from its MQTT traffic, at the cost of the measurement covering **both**
hops through the broker (relay↔broker *and* broker↔device), not the single relay↔broker hop §7.2's
table models. A from-scratch theoretical figure was computed in parallel, using §7.1's exact stated
rules (MQTT PUBLISH/PUBACK sizes, +40 B TCP/IP per segment, no TLS) applied to the *actual* payload

**Results** (20 down messages fully acked + 5 replies; bodies ~50–100 bytes, longer than §7.2's
example on purpose, to make sure the comparison isn't an artifact of one convenient string length):

| Quantity | Value |
|---|---|
| Theoretical, MQTT bytes only, both hops, no TLS | 14,420 B |
| Theoretical, MQTT + 40 B/segment TCP/IP, both hops, no TLS | 24,820 B |
| **Measured, `docker stats` NetIO delta on the broker container** | **42,870 B** |
| measured ÷ theoretical (MQTT+TCP/IP, both hops) | **1.73×** |

Re-expressed as a single hop (measured ÷ 2, since the two hops carry near-identical payloads —
this is the number comparable to §7.2's per-exchange table, which models one hop):

| Quantity (single hop, no TLS unless noted) | Value |
|---|---|
| Computed, down message fully acked (down + `shown` + `read`), this run's payload sizes | 688 B |
| Computed, student reply, this run's payload sizes | 248 B |
| §7.2's worked example, same exchange, **with TLS** | 289 B |
| Theoretical single-hop total for the whole 20+5 run (§7.2's 3-frame-per-exchange method, no TLS) | 15,010 B |
| Measured-implied single-hop total for the whole run (measured ÷ 2) | 21,435 B |
| ratio (measured-implied ÷ theoretical, single hop) | 1.43× |

**Assessment: consistent, not an exact match, and the gap is explained.** The measured bytes are
higher than the theoretical estimate by 1.4–1.7× depending on how the two hops are split, in the
direction expected, for reasons the theoretical model explicitly doesn't try to capture:

1. **Ethernet framing.** `docker stats` counts bytes at the container's virtual NIC, which includes
2. **TCP ACKs are not perfectly piggybacked.** §7.1 already flags this ("bare TCP ACKs counted at
3. **Docker's bridge/NAT path adds its own small overhead** (veth pair + bridge + userland-proxy or
4. **Missing TLS pushes the other way, and is a separate comparison from point (1)–(3) above.**
5. **This measurement cannot validate the TLS or cellular assumptions at all**, by construction —

**Verdict.** The measurement confirms §7.1/§7.2's core claim — MQTT PUBLISH/PUBACK sizes computed
from JSON payload lengths plus fixed per-field overhead are the right order of magnitude and the
right formula — to within the 1.4–1.7× gap explained above, all of which is either (a) real overhead
the theoretical model intentionally simplified away (Ethernet framing, imperfect ACK piggybacking)
or (b) an artifact of the local Docker bridge that has no cellular equivalent. Since §7.3's monthly
projections (1.69 kB/day nominal, 236 kB/day pessimistic) sit at roughly 3% and 7% of the 100 MB
cap respectively (§7.3's own "~59×" headroom claim), even a 1.7× correction to the message-only
terms — the reconnect term dominates the pessimistic case and this measurement says nothing new
bar. **No change to §7.3's verdict is warranted by this measurement**; it is recorded here as
supporting evidence, not a revision.

---

## 8. ESP32 wake sources


### 8.0 The library constraint that forces the design

Three facts, each checked in the **v1.5.0** source, not assumed:

1. **Incoming messages are announced only by a live URC, and the announcement carries data you
2. **None of that state is in RTC memory.** `_sleepPrepare()` (`src/WalterModem.cpp:4068-4095`)
3. **There is no way to ask the modem what it is holding.** `AT+SQNSMQTTRCVMESSAGE` needs the

message that arrives while the ESP32 is in deep sleep is announced into a powered-down UART and is
then unreachable through the public API.** Deep sleep does not merely fail to wake us — it loses the
message, and it loses the `mid` needed to ever ask for it again.

holding while RTS was deasserted, the library's RX task parses it, the event task dispatches it, the
handler calls `mqttReceive()`. The ESP32 issues no speculative AT traffic at all. The energy model in
§8.2 is unaffected: what matters is the length of the awake window and how often it happens, not what
the ESP32 does inside it.


- **A real event system, dispatched from a dedicated task.** `setMQTTEventHandler()`
  (`src/WalterModem.h:6011`) delivers `WALTER_MODEM_MQTT_EVENT_MESSAGE` / `_CONNECTED` /
  `_DISCONNECTED` / `_SUBSCRIBED` / `_PUBLISHED` / `_MEMORY_FULL` (`:1368-1375`) from
  `_eventProcessingTask` (`src/WalterModem.cpp:1595-1626`), **not** from the RX ISR/task. Calling
  modem APIs from the handler is supported and is what the vendor example does. Note the task
  polls its queue on a 10 ms tick and adds a further 10 ms settle delay before dispatch, so budget
- **`+SQNSMQTTMEMORYFULL` → `WALTER_MODEM_MQTT_EVENT_MEMORY_FULL`** (`src/WalterModem.cpp:3398-3405`)
  tells us the modem's own message buffer overflowed. That is the direct signal that the
  wake-and-drain cycle is falling behind, and firmware should count it (§8.4).

### 8.1 Wake source table

| # | Wake source | Mechanism | Trigger | Mode | Verified? | RTC state that must survive |
|---|---|---|---|---|---|---|
| 1 | **Wake-and-drain timer** (primary) | `esp_sleep_enable_timer_wakeup(T)` + `esp_light_sleep_start()`, `T` = 5 s sleep / 2 s active | Wake, reassert RTS, let the modem flush the URC it held, let the library's RX and event tasks dispatch it to the MQTT handler, which calls `mqttReceive(topic, mid, …)`. **No polling call is made** (§8.0). | both | Mechanism yes; the RTS hold-off behaviour is **not verified** (§8.3) | Light sleep retains RAM, so *nothing* has to survive — this is the point |
| 4 | RTC timer — active-mode exit | `esp_timer` while awake, not a sleep wake | 10 min with no button/keyboard activity → sleep mode | active | Yes | `mode`, `active_until` |
| 4b | MQTT event (no sleep involved) | `setMQTTEventHandler()` → `_eventProcessingTask` | `_MESSAGE` short-circuits the wake-and-drain latency while the ESP32 happens to be awake; `_DISCONNECTED` drives recovery; `_MEMORY_FULL` flags a missed drain | both | Yes — v1.5.0 API | none (handler runs while awake) |
| 7 | CardKB | — | **Cannot wake the ESP32.** No interrupt line to an RTC GPIO; polled at 100 ms only while the composer is open. A reply always starts with a button press. | active | Yes (by construction) | — |

modem UART is board-fixed inside the component — RX 14, TX 48, RTS 21, CTS 47, RESET 45
(`src/WalterModem.cpp:82,87,92,97,102`, Kconfig-overridable but correct for Walter as shipped). None of those
collide with `firmware/main/pins.h` (1, 2, 8, 9, 10, 11, 12, 15, 16, 17, 18), and `begin()` takes
only a `uart_port_t`. No pin needs to be added to `pins.h` and **no GPIO reassignment is requested**.

### 8.2 Why light sleep, and why `T` = 5 s — the arithmetic

(Terminology: `T` is the **wake-and-drain interval**, not a polling interval — see §8.0. The energy
model is indifferent to that distinction; the latency model in §6.5 is not.)

Vendor figures, from the `WalterModem::sleep()` doc comment (`src/WalterModem.h:4269-4271`) — these
are **vendor-documented board-level numbers, not this document's estimates**: light sleep **1 mA**,
deep sleep **9.5 µA**. Assumption attached to both: they describe the board with the modem also
asleep. Our modem stays attached in eDRX, so the modem paging term is added on top of both and
cancels out of the comparison below.

Per-wake energy, ESP32 side, at an assumed **40 mA** active draw (ESP32-S3, radio off):

| | awake time per cycle | why |
|---|---|---|
| Deep-sleep cycle | **600 ms** (estimate) | Deep sleep restarts program execution. Bootloader + `app_main` ≈ 300 ms, then `WalterModem::begin()` must re-open the UART, spawn its tasks, run `_sleepWakeup()` — which itself issues a `checkComm()` (`src/WalterModem.cpp:4097-4100`) — and then `configCMEErrorReports()` + `configCEREGReports()`, i.e. at least three AT round trips (`src/WalterModem.cpp:4258-4276`). |

ESP32-side average current as a function of the poll interval `T`:

```

```

30 − 20.48 − 0.3 − 1.5 = **7.7 s**. The two requirements do not overlap, so light sleep wins, and it
is not close:

| `T` | `I_light` | `I_deep` | sleep-mode worst-case latency |
|---|---|---|---|



Two variants could make deep sleep safe, and both are blocked on something not yet verified:

- **(a) Hold RTS deasserted across deep sleep.** RTS is GPIO21, an RTC GPIO, so `rtc_gpio_hold_en()`
  plus `gpio_deep_sleep_hold_en()` can pin it high while the ESP32 is down. If the Sequans honours
  CTS and queues its URCs instead of dropping them, the URC is delivered intact after
  `WalterModem::begin()` releases the hold — which is exactly the trick the library already performs
  for light sleep (`src/WalterModem.cpp:4413-4450`). This fixes *correctness*, but it does not fix
  *energy*: the 600 ms re-init per cycle still applies, so the table above still says deep sleep
- **(b) `ext1` wake on the modem RX line (GPIO14) + forced redelivery.** Wake on the URC's start
  bit, accept that the URC bytes are lost, then `mqttDisconnect()` + `mqttConnect()` so the broker
  redelivers the unacked QoS 1 message while the ESP32 is awake. Energy would be excellent —
  9.5 µA floor plus roughly 0.107 mAh per message (600 ms boot at 40 mA, plus ~3 s of RRC at
  ~120 mA for the handshake), so about **12–19 mAh/day** against the 38–50 mAh/day of §8.4, a 2.5–4×
  win. Data cost is ~5 kB per message (§7.2), ~100 kB/day at 20 messages — irrelevant against the
  3413 kB/day the SIM allows. **But it is entirely dependent on `cleanSession=false`, which the
  library cannot set (§6.1).** If the Sequans defaults to a clean session there is no redelivery and
  the message is simply lost. Do not implement (b) until the §6.1 clean-session experiment has
  passed. It is the highest-value follow-up in this document.

Note that (a) and (b) are mutually exclusive: holding RTS high stops the modem transmitting, so the
RX line never toggles and the `ext1` wake never fires.



### 8.4 Sleep-mode current estimate


| Term | Value | Assumption |
|---|---|---|
| ESP32-S3 light sleep, board level | **1.00 mA** *(vendor)* | `WalterModem::sleep()` doc comment; RAM retained, UART domain powered |
| Modem idle floor | 0.01–0.05 mA (estimate) | Sequans GM02SP deep-sleep-between-paging |


Active mode, 10-minute window: the ESP32 light-sleeps at `T` = 2 s between drains rather than
spinning, so its contribution is `I_light(2)` ≈ **2.0 mA**, not the ~35 mA of a busy-wait. The modem
holding RRC/C-DRX dominates at an estimated 5–15 mA, giving **7–17 mA → 1.2–2.8 mAh per window**.
Six windows/day ≈ **7–17 mAh/day**, so active mode adds roughly 20–40 % on top of sleep mode rather

Firmware MUST count `WALTER_MODEM_MQTT_EVENT_MEMORY_FULL` events and publish the counter in the
`/status` heartbeat if it is ever non-zero: it is the only direct evidence that the wake-and-drain
cycle is losing messages, and it is cheap.


## 9. Storage contract: RTC memory vs. ordinary RAM


### 9.1 The measured budget

ESP32-S3 RTC slow memory is **8192 B** (`rtc_slow_seg`, `0x50000000`, length `0x2000`). All figures
below are read from `firmware/build/school_pager.map` after a clean

| Consumer | Bytes | Map symbol |
|---|---|---|
| `walter-modem` v1.5.0 (`_pdpCtxSetRTC`, `_mqttTopicSetRTC`, `_socketCtxSetRTC`, `_coapCtxSetRTC`, `blueCherryRTC`) | **7003** | `.rtc.data.0-.4` under `WalterModem.cpp.obj` (`0xc80 + 0x910 + 0x3c0 + 0x208 + 0x3`) |
| IDF/linker remainder | 5 | `_rtc_slow_length` (`0x1f58`) minus `.rtc.data` (`0x1f54`) |
| **Available to the pager** | **1184** | `0x2000 - 0x1f58 + sizeof(g_rtc)` |

Reclaiming the library's 7003 B is not an option: `CONFIG_WALTER_MODEM_ENABLE_SOCKETS`/`_HTTP`/
`_COAP`/`_BLUECHERRY` each **fail to build the vendor library itself** (unguarded references in
component. See `sdkconfig.defaults` for the full finding.

### 9.2 The split, and the rule that decides it

> **Rule.** A datum lives in RTC memory if losing it to a reset would make the relay's view of
> delivery *wrong* — i.e. the relay believes a message was delivered or a reply was sent when the

Under that rule three things are RTC-resident: the **dedup digest ring** (§4.1 rule 7 — without it a
post-reset redelivery storm costs a full active-mode window per duplicate), the **pending ack queue**

### 9.3 RTC-resident layout (`pager_rtc_t`, owned by `modes.c`)

| Field | Bytes | Purpose |
|---|---|---|
| `boot_count` | 4 | Diagnostics; distinguishes cold boot from reset recovery |
| `session_id[12]` | 12 | §1; regenerated **only** on cold boot |
| `mode`, `active_until_epoch` | 16 | Mode state machine |
| `status_pub_count`, `last_status_epoch` | 16 | §5.4 heartbeat cadence |
| `mqtt_memfull_count`, `oversize_drop_count`, `modem_resets`, `last_modem_reset_us`, `attach_fail_cycles`, `wake_cycle_count` | 32 | §8.4, §3.4 counters |
| `msg.seen_ids[16]` (`uint32_t`) + `seen_head` | 68 | Dedup ring (§4.1 rule 7) — **a 32-bit digest of the id, not the id string.** Message ids carry 32 bits of entropy by construction (§1), so the digest is the id's own randomness; 16 entries collide with probability ≈3×10⁻⁸, and the only consequence of a collision is one suppressed render. Costs 68 B where the literal strings cost 276 B. `id_hash == 0` means "empty slot"; a real digest of 0 is stored as 1. |
| `msg.pending_acks[8]` (`id[17]`, `state`, `attempts`) | 152 | Ack retry queue (§4.1 rule 6). Ids are stored in full — the ack has to put the real id on the wire. |
| `msg.dedup_hits`, `msg.malformed_drops`, `msg.reply_failed` | 12 | Diagnostics for the `/status` heartbeat |

`modes.c` remains the sole owner of the struct, its single `magic`/`crc32` pair and `rtc_save()`
(§11's "one transition funnel" discipline applies to RTC writes too). `msg.c` receives a typed
pointer to the nested `msg` sub-struct and calls back into `modes.c` to re-CRC. `modes_boot()` MUST
log `sizeof(pager_rtc_t)` at boot and MUST `_Static_assert(sizeof(pager_rtc_t) <= 1184)`.





| Field | Bytes | Purpose |
|---|---|---|


### 9.6 What survives what


Concretely, after a **crash or watchdog reset**: pending acks and pending replies are retried
normally, dedup still suppresses redeliveries, and the newest unread message is re-rendered from its NVS copy (§9.4). The thread is restored from the `msghist` partition (§9.5) for every record that passes its version and CRC check. If no record is restored, the thread starts empty and `msg.c` arms a history-lost flag; `ui.c` MUST then show an explicit "earlier messages lost (restart)" line. *Not implemented:* no screen reads the flag yet, so the line is not shown.

After a **cold boot** the RTC struct is invalid, so `session_id` is regenerated, which §5.3 turns
into a session change at the relay and therefore a re-publish of everything still `queued`/`sent`.
Dedup is empty at that point, so those messages render normally. Cold boot is the *better*-recovered
case of the two.

> §5.3's re-publish selection from `state in (queued, sent)` to
> `state in (queued, sent, shown) AND age < 2 h` on a **session change only** would let the relay
> recover *all* unread messages after a cold boot, not just the ones that never reached `shown`, and
> would make `msg.unread[]` redundant rather than merely shallow. It costs nothing in the common
> case (dedup suppresses re-render when RTC survived; a session change only happens on cold boot).

### 9.7 Standing rules

- Firmware MUST NOT assume the modem's MQTT session state is mirrored in RTC memory. On every wake
  it re-reads the modem's actual connection state and drains the URC the modem was holding (§8.0).
- **Do not shrink `seen_ids`** below 16 entries — see the duplicate-storm cost in §4.1 rule 7.
- Keeping this layout in RTC rather than moving all of it to RAM still buys the two things §8.3(b)
  needs: if the clean-session experiment (§12 item 4) passes and deep sleep returns, the durability
  half of the store already has the right shape and the change is a sleep-call swap, not a data-model
  rewrite.
---


---

## 11. Future extension points (do not design now)


**Topic namespace**
| Reserved topic | Intended use | Notes |
|---|---|---|
| `pager/{id}/evt` | geofence enter/exit, motion, low-battery alerts | Separate from `/up` so the parent thread stays human messages only |


**Enum headroom** — `from` already allows `system`, which is where geofence and low-battery
specifically so `school` / `travel` / `night` can be added later; every consumer MUST treat an
unknown `mode` value as `sleep` for display purposes rather than erroring.

→ sleep; sleep → active on incoming message or button; active → sleep after 10 min idle. Later
schedule- or geofence-driven switching enters at the same `set_mode()` edge, driven by `/cfg` and
`/evt`, so `modes.c` MUST funnel every transition through one function with a reason code rather

---

## 12. Open questions


3. **`NEEDS HUMAN DECISION` — LWT and clean session are not settable from `walter-modem` v1.5.0.**

4. **Not a decision, but the highest-value experiment in this document.** Settle whether the modem's


7. **`NEEDS HUMAN DECISION` — should the relay re-publish `shown`-but-unread messages on a session


### Unverified assumptions tracked in this document

| § | Assumption | Status | Cheapest experiment |
|---|---|---|---|
| 6.1 | `walter-modem` MQTT is 3.1.1, not 5 | **Yes**: 3.1.1; no version/property API exists | — |
| 6.1 | `walter-modem` exposes TLS session resumption | **No.** Costs the ~40 % saving on the pessimistic data budget (§7.3) | — |
| 6.1 | A CA can be provisioned from application code | **Yes**, `tlsWriteCredential()` is public in v1.5.0; cert slot ≥ 11, TLS profile ≥ 2 | — |
| 6.1 | The modem's MQTT session is `cleanSession=false` | **Not verified; load-bearing.** Not settable, so it is whatever the Sequans defaults to — and `mqttConnect()` clearing the subscription table hints at "clean" | §12 item 4, ~20 min on hardware |
| 6.1 | An LWT can be registered | **Not verified**; no typed API | Raw `AT+SQNSMQTTCFG` with will parameters via `sendCmd()`; check the Sequans AT manual first, 30 min, no hardware |
| 6.3 | eDRX API takes seconds vs. the raw 3GPP nibble | **Yes**: raw 4-bit binary nibble strings, spliced verbatim into `AT+SQNEDRX` | — |
| 6.3 | The granted eDRX/PTW values are readable | **Yes**, `setNetworkEventHandler()` → `WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED` → `data.edrx.nwProvidedEdrx` | — |
| 6.3 | Carrier grants the requested 20.48 s / 2.56 s | Not verified; §6.5 breaks if it grants 40.96 s | Assert on `nwProvidedEdrx` at attach — free, first bring-up |
| 8 | Modem URC can wake the ESP32 from deep sleep with the message still retrievable | **No**, not via the public API. §8.0. This is the correction that drives §8.4 | — |
| 6.5 | SSD1680 partial refresh completes in <1.5 s at room temperature | **Not verified** — §6.5 breaks if it does not; only 2.7 s of slack at `T`=5 s | Toggle a GPIO around `ui_refresh()`, scope the pulse width; also log `esp_timer` deltas around the BUSY wait |
| 9.4 | The M5Stack CardKB emits one ASCII byte per keypress (no multi-byte sequences) | **Not verified; load-bearing for §9.4** — if it can emit >0x7F, a 160-byte RTC reply slot is no longer 160 characters | Poll 0x5F over I2C, dump every non-zero byte for a full pass over the keyboard incl. Fn/sym combos; 15 min on hardware |
| 12.6 | `getVoltage()`/`AT+SQNVMON` reports real battery voltage, not a fixed regulated rail | **Not verified; cheap to confirm** — inferred from Walter's public schematics (§12 item 6), not from Walter's own unpublished internal routing | Compare `getVoltage()`'s reported `batt_mv` against a multimeter reading of the actual battery on first hardware bring-up; 5 min, no code change either way |
