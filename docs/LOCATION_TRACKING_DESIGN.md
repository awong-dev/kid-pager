# Location tracking: hourly cell fix when stationary, cell + occasional GNSS when moving

Background tracking reports the pager's position without a web request. While stationary it sends
one cell fix an hour. While moving it sends cell reports on cell changes and tries GNSS every 10
minutes. Builds on `V02_DESIGN.md` §5 (on-demand location) and §9 (liveness ping); the wire contract
is `PROTOCOL.md` §13. Every mAh figure is an **estimate** unless it says **measured**, and each one
names its source.

## Decisions

1. GNSS is attempted **in place only** (LTE attached, eDRX gaps), through the library `gnss*` API
   (`AT+LPGNSSCFG/FIXPROG/ASSISTANCE`, `firmware/components/dptechnics__walter-modem/src/proto/WalterGNSS.cpp`).
   Fresh assistance is mandatory, refreshed at most every 2 h and only on days with GNSS.
2. **Any GNSS failure → send a cell report and stop.** That covers `LTE_CONCURRENCY`, timeout,
   no fix and confidence > 100, for scheduled fixes and web requests alike. There is no second
   route (see §7).
3. GNSS while moving every **10 min**.
4. Passing periods are **cell only**: the first attempt waits one full interval into motion.

## 1. State machine

Two states, STILL and MOVING, held in RAM (the device starts in STILL after a reset). A **new
cell** is one not in a 4-entry ring of distinct cells seen in the last 60 min; the ring stops an
indoor modem flapping between cells A and B from looking like motion.

| Transition | Condition |
|---|---|
| STILL → MOVING | accel sustained-motion fires (classifier: events spanning ≥60 s in 3 min), **or** 2 new cells within 15 min |
| MOVING → STILL | no accel edge for `LOC_STILL_S` = 300 s **and** no new cell for 900 s |

- The cell rule counts as motion even with the LIS3DH fitted: its 256 mg high-pass threshold may
  miss smooth vehicle motion. Not verified. Without a LIS3DH, only the cell rule applies.
- **The backoff resets on STILL → MOVING only**, and the 10 min `loc_req` floor is set on the same
  transition. The `loc_trigger_*` calls are state inputs; otherwise indoor "motion" would keep
  zeroing the backoff.

## 2. Schedule

### 2.1 Reports (all unsolicited, QoS 0, `req:null`)

| Report | `why` | When | Radio cost |
|---|---|---|---|
| Hourly cell | `still` | First uplink window (§4) once ≥3300 s since the last `/loc` of any kind | Rides an existing uplink |
| Cell change | `cell` | A new cell serving for ≥60 s, ≥`LOC_CELL_REPORT_MIN_S` = 120 s after the previous report | Sends the keep-alive early (§4) |
| Moving refresh | `move` | MOVING and no `/loc` for 600 s | Rides the next keep-alive |
| GNSS in motion | `gnss` | §2.2. A fix gives lat/lon plus cell; any failure gives cell only (`loc:null, err:"no_fix"`) | Attempt + early keep-alive |
| End of motion | `stop` | MOVING → STILL, only if the cell differs from the last reported one or a GNSS fix was published this episode | Sends the keep-alive early |

**Every unsolicited report, whatever its `why`, is ≥120 s after the previous one** (`PROTOCOL.md`
§13.3 item 9). A report that falls due sooner waits for the 120 s mark; a newer due report
replaces a waiting one. Answers to a `loc_req` (QoS 1) are outside this floor.

Reports send only while the session is usable, the coverage duty cycle does not own the radio,
and `loctrack` is on (§9). Cell reports ignore the battery floor.

**End of motion is a cell report, not GNSS.** The resting place is the most useful point, but
STILL is declared 5 min after motion ends, when the child is most likely indoors.

### 2.2 GNSS while moving

- **Interval.** `LOC_MOVE_GNSS_S` = 600 s (runtime tunable, 0 = off). The first attempt runs one
  full interval after MOVING starts, so a passing period gets cell reports only. Each attempt also
  needs an accel edge in the last 60 s, or a new cell in the last 600 s when there is no LIS3DH, so
  the exit tail does not attempt.
- **Assistance (mandatory).** Before any attempt, scheduled or requested: if the last refresh is
  ≥2 h old, or `gnssGetAssistanceStatus()` says it is due, call `gnssUpdateAssistance()` while
  connected. The attempt that follows gets the existing 40 s budget. A failed refresh means no
  attempt, and counts as a failure. For a web request the refresh counts inside `PROTOCOL.md` §13.3
  item 2's 60 s: the budget is min(40 s, 60 s − refresh time), and a refresh still running at 60 s is
  a failure (cell answer). Days without GNSS never refresh.
- **Failure handling.** One `gnssPerformAction(GET_SINGLE_FIX)`. Any failure publishes the cell
  and advances the backoff. Daily cap: 48 scheduled attempts (a backstop against a stuck INT1).
- **If in-place is refused on a carrier,** each attempt returns `LTE_CONCURRENCY` at once and
  costs one cell report plus ≤1 refresh per 2 h. The doubling backoff (to 12 h) bounds it.

### 2.3 Interaction with the existing policy

- **Backoff.** The next scheduled attempt runs at max(last attempt + 600 s, backoff). A failure
  steps the backoff 5→10→20→40 min; a success resets it. Indoors that gives attempts at 10, 20,
  30, 50 and 90 min.
- **Trigger floor (10 min).** It applies to `loc_req` only; the move schedule is its own rate limit.
- **One in flight.** A scheduled attempt holds the slot with an empty queue. A `loc_req` arriving
  meanwhile queues and shares the result. On completion, queued requests get their QoS 1 answers;
  with no queue, one unsolicited `why:gnss` is published.
- **Web request.** It always tries GNSS in place, subject to backoff, floor and battery floor. On
  failure it answers `loc:null, err:"no_fix", cell`.
- **Battery floor (3.3 V).** It blocks all GNSS; cell reports continue.
- **Session down at completion.** The publish fails and nothing is retried. A requester sees the
  relay's 15 min expiry.

## 3. Encoding a cell-only report

The existing `/loc` envelope carries it: `{v, id, ts, loc:null, req:null, err:"no_fix", cell:{…},
why, n, sig}`. `err` stays `no_fix`: `wire.py` `LocEnvelope.err` is `Literal["no_fix","disabled"]`
and `PROTOCOL.md` §13.2 requires `err` when `loc` is null, so a new value would make the relay drop
the envelope. `why` is a new optional tstr, key 60 (§5).

Sizes, **measured** with `relay/app/wirecbor.encode()` plus the 10 B `sig`:

| Envelope | Payload | On the wire (MQTT + TLS 29 + TCP/IP 40 + server ACK 40) |
|---|---|---|
| cell only (with `why`) | **79 B** (85 B) | ≈ 0.21 kB |
| GNSS fix + cell (with `why`) | **99 B** (105 B) | ≈ 0.23 kB |

**Relay behaviour for an unsolicited cell-only `/loc`** (`location.py` `ingest_loc`): it writes
`status.lastCell`, resolves the cell via the `cellgeo` cache or provider, stores a `src:"cell"` doc
with `reqId:null`, and dedups on `id`. The design relies on the following fixes (§5, R1, R2, R6–R8):

- `req_ref` is built whenever the device's owner user is known, so every report costs an extra transactional read
  and logs a misleading `late /loc … dropped`. Build it only when the envelope has a `req`.
- `locate()` rule 6 answers a web request from any fix under 60 s old, so a cell report just before
  a web request would suppress the GNSS attempt the design requires.

The signature is over raw bytes (`devauth.py`) and the replay window is 64 wide, so key 60 and QoS 0
reordering are safe. Three existing weaknesses that tracking's ~24–100× `/loc` volume makes matter:
`locWireIds` is keyed by the bare 32-bit random `id` across all devices (`location.py`), so a
collision silently drops a report; `status.lastCell` is overwritten by whichever webhook lands last,
so an out-of-order QoS 0 report regresses it; and with the default `cellgeo` provider `none`, a
cell-only report stores no fix at all, so the map shows nothing new.

## 4. Piggybacking on the keep-alive

Rule: **the SUBSCRIBE stays the liveness proof. `/loc` rides the same RRC connection and never
replaces or delays it.**

1. `net_set_uplink_window_cb(cb)` fires on `modes_run()`'s task right after a liveness SUBSCRIBE
2. If a report is due, `loc` forces an `AT+SQNMONI` refresh (to catch a lost `+CEREG`) and
   publishes at once, inside the RRC inactivity tail, so there is no new radio wake.
3. **Unsolicited QoS 0 publishes leave `s_last_uplink_us` alone.** QoS 0 proves nothing about the
   session, and resetting the idle clock delays dead-session detection (`V02_DESIGN.md` §9.2).
4. Urgent reports (`cell`, `stop`, `gnss`) call `net_liveness_ping_now()`: the SUBSCRIBE goes early
   through the normal verdict path, the report follows it, and the next ping is timed from this
   one (a ping replaced, not added). If a SUBACK is outstanding, the report waits. Lost reports
   are not retried.

Costs (estimates): a report on an existing wake, **C_ride ≈ 0.02 mAh** (≤0.5 s more RRC tail at
~120 mA plus ~0.2 s of ESP32 at 40 mA; `V02_DESIGN.md` §9.3, `PROTOCOL.md` §8.4). A ping sent early,
**C_early ≈ 0.07 mAh** (0.02–0.12): about half a 0.1 mAh ping (not measured) plus C_ride.

## 5. Protocol and relay changes

- **P1** `PROTOCOL.md` §13.3, normative item 9 (SHOULD, not MUST, because QoS 0 reports are not
  retried and the relay infers nothing from a missing one): "A device MAY publish unsolicited `/loc`
  (`req:null`, QoS 0) at most once per 120 s, and SHOULD send one at least every `loc_period_s` while
  its session is usable. A scheduled GNSS attempt obeys items 1–3 like a requested one and shares
  its result with any `loc_req` arriving during it. A web `loc_req` is never answered from an
  unsolicited `src:"cell"` fix." §13.2 `no_fix` means "this envelope carries no GNSS position; with
  `req:null` and `cell` it is a cell-only report". Item 2's 60 s bound holds (budget ≤40 s).
- **P2 `loc_period_s`.** 3600 while tracking is on, meaning the maximum gap between unsolicited
  reports while connected. 0 means off. The current mode travels on each `/loc` as `why`, not in
  `/status`, because putting it in `/status` would force an extra publish.
- **P3** new optional keys: `why` = 60 (tstr on `/loc`: `still|cell|move|gnss|stop`), `loc_move_s` = 61
  (int on `/status`). Unknown keys are ignored (§3.1), so either side can ship first.
- **R1** `ingest_loc`: build `req_ref` only when `env.req is not None`.
- **R2** `locate()` rule 6: an unsolicited `src:"cell"` fix (`reqId` null) never satisfies the
  60 s cached window. A cell fix that answered a request still satisfies it; otherwise a second
  requester 30 s later would trigger a second `/down` inside 60 s, breaking `PROTOCOL.md` §13.3 item 7.
- **R3 storage:** ~24 docs/day stationary, ~100 on a heavy day, ≤700 per device at 1-week
  retention. Proposal: when the newest doc is `src:"cell"` with the same cell, update its `lastTs`
  instead of inserting (the trail shows dwell, not 24 pins). Store `why` on each doc. The doc must
  store the cell identity (`cellKey`) to compare; a dwell doc stops extending once its `createdAt`
  is 24 h old (the sweep deletes by `createdAt`, so an unbounded dwell would be swept while it is
  still the newest point); the web card ages a dwell doc by `lastTs`, not `fixTs`.
- **R6** relay mirror of the 120 s floor: an unsolicited report <60 s after the previous
  unsolicited doc for the device skips the provider call and the new doc (it still updates
  `lastCell`). **R7** `lastCell` is written only if its `ts` is not older than the stored one.
  **R8** the `/loc` dedup key becomes `{deviceId}_{id}`.
- **Privacy.** Today `locatableBy` users see points only when someone asks; with tracking on they
  see an hourly trail. The rules are unchanged and still relay-write-only, but the meaning of "may
  locate" widens. `loctrack` defaults to off.
- **R4 provider cost.** A provider is called only on a cache miss (a cell's first sighting), about
  30 on a heavy day. The per-call price is not known.
- **R5** `tools/pager_client.py` emits `still` and `cell` reports for the e2e suite.

## 6. Power per day

Added to the 95–107 mAh/day sleep baseline (`PROTOCOL.md` §8.4, estimate).

Unit costs (all estimates):
- **GNSS current 30 mA**: no measured figure exists for the GNSS receiver; 30 mA is a placeholder
  (same as `PROTOCOL.md` §12 item 8).
- **Attempt, 0.2 mAh average** (50 % success assumed while moving). Success: ~10 s × 30 mA +
  C_early = 0.15. Timeout: 20 s × 30 mA + C_early (cell report) = 0.24, or 0.40 at the 40 s
  post-refresh budget. `LTE_CONCURRENCY` refusal: 0.07 (the cell report).
- **Refresh, 0.4 mAh:** 0.3 for the download (size not known) plus 0.1 for the longer budget of the
  attempt that follows.
- **Motion sensing, 0.4 mAh per hour moving:** ≤3 extra ext1 wakes/min at 200 ms × 40 mA. The
  LIS3DH's ~6 µA (datasheet) is negligible.

Days: **School** = 2 × 30 min commutes, each with 4 new cells, 1 `stop` and 3 attempts at
10/20/30 min, plus 2 refreshes. **+ 6 passing periods** adds 6 × 7 min of motion, no reports.
**Heavy** = 3 × 60 min, 10 new cells/h, 6 attempts per episode, 3 refreshes. In **All fail**
every attempt times out and the backoff thins them (a 60 min episode gets 4).

| Day | Cell reports | Motion sensing | Assistance | GNSS | **Total** | All fail at timeout | All refused |
|---|---|---|---|---|---|---|---|
| Stationary | 24×0.02 = 0.5 | 0 | 0 | 0 | **+0.5** | +0.5 | +0.5 |
| School | 0.5 + 2×6×0.07 ≈ 1.4 | 0.4 | 2×0.4 = 0.8 | 6×0.2 = 1.2 | **+3.8** | +4.2 | +2.8 |
| School + 6 passing (a) | 1.4 | 0.7 | 0.8 | 1.2 | **+4.1** | +4.5 | +3.1 |
| Heavy | 0.5 + 36×0.07 + 0.1 ≈ 3.1 | 1.2 | 3×0.4 = 1.2 | 18×0.2 = 3.6 | **+9.1** | +8.6 | +6.0 |

That is +0.5 % to +9 % over the baseline. Each web request that runs GNSS adds 0.15–0.4 mAh, plus
0.4 mAh if a refresh is due.

Data: a heavy day is ~8 kB of cell reports and ~4 kB of GNSS reports, ≈0.4 MB/month plus
assistance downloads. That is far inside 100 MB.

Unknowns, in order: whether in-place works on this modem and carrier; GNSS current and time to fix;
assistance size; the 0.1 mAh ping.

## 7. Route 2 is not used

The earlier design had a second route after a failed in-place attempt: drop the modem's radio
(`CFUN=4`), fix, and re-attach. It is removed. Its costs were about 2.5 minutes unreachable per fix
(re-attach measured about 2 minutes on AT&T) plus a TLS reconnect, which breaks the ~30 s delivery
target. A failed attempt sends the cell report and stops (decision 2).

What remains of that design: `net_radio_off()` and `net_radio_on()` stay, because the coverage duty
cycle uses them. The RTC route byte is kept as `_reserved` rather than deleted, because removing the
struct would change the RTC layout and re-initialise the whole structure (auth counter and lock state
included) on the next boot.

## 8. Rejected alternatives

- **CFUN=4 window:** see §7.
- **PSM window:** PSM stops paging; the pager would be unreachable ~30–65 s per fix, and the carrier
  grant and MQTT survival are not verified.
- **Retry or second route after a failure:** rejected; the cell report is the answer.
- **Early first fix in passing periods (option b):** rejected in favour of option (a); it would cost
  ~0.3 mAh per mostly-indoor passing period.
- **`AT+SQNAGNSSDATA`, `AT+SQNGNSSCFG="priority"`, `AT+GNSSSTART`:** not in the library.
- **GNSS while stationary or at end of motion:** the pager is almost always indoors then.

## 9. Verification plan

Runtime flags `loctrack on|off` (default off until the stationary soak passes) and
`locmove <s>` allow an A/B comparison on one flash: A = off (cell-only behaviour before tracking),
B = on.

- **Host tests** (`firmware/host/test_loc.c`): transitions; the flap ring (A/B/A never enters
  MOVING); transition-only backoff reset; schedule against backoff; daily cap; every failure kind
  → cell report; queue sharing; `why` encoding cross-checked with `relay/.venv/bin/python`.
- **Relay tests:** an unsolicited cell `/loc` creates one doc with no `locReqs` read; `locate()` 10 s
  after a cell report still dispatches `loc_req`; `locate()` 10 s after a cell-resolved answer is
  still served from the cache.
- **Stationary soak, 1 h each for A and B:** B shows exactly one `why=still` ≤1 s after a liveness
  SUBSCRIBE, the broker ping cadence matches A, and the relay stores one `src:"cell"` doc. With a
  current trace this measures C_ride.
- **Missed `+CEREG`:** the forced `AT+SQNMONI` agrees with `AT+CEREG?`. The trace should show no RRC
  activity from `SQNMONI` in idle. Not verified.
- **Motion:** shake the unit for 6 min with `locmove 120`. Expect MOVING, then an attempt at +120 s,
  then a cell report if that attempt fails, then STILL 300 s after the last edge. Then a 5 min car ride
  checks the 256 mg threshold.
- **In-place GNSS outdoors** (the key experiment): `gnsstest 40` while attached in eDRX with fresh
  assistance. Record `LTE_CONCURRENCY` or not, time to fix, confidence, whether the MQTT session stays
  up, and the latency of a page sent mid-attempt against 30 s. With a trace: GNSS mA and mAh. If the
  modem refuses in-place GNSS, scheduled GNSS is not useful on that carrier: set `locmove 0`.
