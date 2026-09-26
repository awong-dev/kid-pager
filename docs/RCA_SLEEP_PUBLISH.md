# RCA: release-only "publish payload replaced by AT command text"

Status: the mechanism is narrowed but not proven. The instrumented experiment in §3 is the test
that settles it. This supersedes the cause given in `docs/GOTCHAS.md`, which names a mechanism
this analysis refutes.

## 1. What is established

**The bad payload is produced by the walter-modem library's own retry, not by `msg_pump()`.**
`_processModemCMD()`, `WalterModem.cpp:1763-1787`: when a `WALTER_MODEM_CMD_TYPE_DATA_TX_WAIT`
command times out (`CONFIG_WALTER_MODEM_CMD_TIMEOUT_MS=30000`, `firmware/sdkconfig`) and
`cmd->attempt < cmd->maxAttempts` (default 3, `WalterModem.h:142`), line 1778 re-transmits **the
same AT command line** with no knowledge of whether the modem is still owed `payloadSize` bytes
from the first attempt. If it is, the modem consumes the first `payloadSize` bytes of that line as
the payload and publishes them.

`AT+SQNSMQTTPUBLISH=0,"pager/<device_id>/up",1` is exactly 44 characters. The relay's logged
`first64` stops at that `1`, so the published payload was exactly 44 bytes. The stuck command
declared 44 bytes, and the bytes that satisfied it were its own re-transmitted command line. 44
bytes is a signed `publish_ack()` envelope (`firmware/main/msg.c`): map(6) + `v` + `id` + `ts` +
`ack` + `n` + a 10-byte `sig` suffix (`auth.h:56,60`). The relay saw it about 35.65 s after the
previous clean ingest, which is one 30 s command timeout plus relay ingest.

**This retry path is unconditional.** No light sleep is needed. Any lost `>` prompt or lost
payload write, from any cause, becomes a published AT command line and a relay `SECURITY bad-sig`.

**Which task issues which publish:**

| publish | task | blocking? | protected from `net_sleep()` today? |
|---|---|---|---|
| `/status` on the incoming-page mode edge | modem **event task** (prio 4): `_eventProcessingTask` → `pager_mqtt_event_handler` MESSAGE (`xport_lte.cpp:277-324`) → `s_msg_cb` → `modes_alert_incoming()` (`modes.c:1195-1218`) → `set_mode()` (`modes.c:802-846`) → `publish_status_online()` (`modes.c:734-755`) | yes | yes, but only incidentally: `s_handler_busy` (`xport_lte.cpp:289,323`) → `net_modem_busy()` → `skip_sleep` (`modes.c:1836`) |
| `/up` ack and reply | **modes task** (prio 1): `modes_run()` → `msg_pump()` (`modes.c:2354`) → `publish_ack()` (`msg.c:2143`) | yes | n/a |
| `/status` heartbeat, sleep edge; `/loc`; `sms_log`; `book` | modes task | yes | n/a |
| `/up` setup ack (`setup.c:827`) | event task | yes | **no** |

`mqttPublish()` is synchronous: no callback is passed, so `_returnAfterReply()`
(`WalterDefines.h:282-296`) blocks the calling task until the command completes.

**The first proposed mechanism is refuted for this failure.** The stuck publish is an ack from
`msg_pump()` on the modes task, and that task is blocked inside `mqttPublish()` for the whole 30 s,
so it cannot reach `net_sleep()` (`net.cpp:1079-1177`). "Our own light sleep during our own
publish" is not the mechanism for an ack. The flag gate that the first fix attempt added is also a
no-op as written: `publish_quiet_gate_issued()` runs after the blocking call returns
(`xport_lte.cpp:409-415` and `436-440`), so the flag never covers the command.

## 2. Believed mechanism: the `>` prompt is orphaned inside the parser

`_parseRxData()`, `WalterModem.cpp:1445-1478`. The prompt is recognised only at buffer offset 2,
that is, only when the buffer holds exactly `\r\n> `:

```c
1449  if(_parserData.buf && _parserData.buf->size > 2 &&
1450     _getCRLFPosition(buf->data + 2, buf->size - 2, true)) { ... _queueRxBuffer(); continue; }
1468  bool prompt1 = (buf->size >= 4 && buf->data[2] == '>' && buf->data[3] == ' ');
```

`_getCRLFPosition(..., findWhole=true)` returns true for a CRLF anywhere in the range, not only at
the end. So if the parser buffer is non-empty when the prompt's leading `\r\n` arrives, that CRLF
terminates the residue, the residue is queued, and the remaining `> ` starts a fresh buffer of size
2. That fails line 1449 (`size > 2`) and fails `prompt1` (`size >= 4`). The prompt is never handed
to `_processModemRSP()`, so the payload write at `WalterModem.cpp:2159-2181` never runs. The modem
sits at the prompt owed 44 bytes, and 30 s later line 1778 feeds it the command line. Traced
byte for byte, the orphan occurs for input `\r\nOK\r\n> ` (one CRLF between the previous line and
the prompt) and for any CRLF-less residue left in the buffer.

Two ways a CRLF-less residue appears, both consistent with "release build only":
- **Bytes lost at the light-sleep boundary.** `net.cpp:1147-1151` disables hardware flow control
  before forcing RTS high, then sleeps. In that gap the modem may send into a UART that is about to
  be powered down, and bytes already in the RX FIFO are lost. A line truncated mid-way leaves
  exactly this residue. The debug build never sleeps, so it never truncates and never orphans,
  which is why this did not reproduce on debug.
- **Read-chunk shape.** On wake the modem dumps what it held while RTS was high, so lines that
  normally arrive one per `uart_read_bytes()` arrive concatenated. Concatenation with a double CRLF
  is handled correctly; concatenation with a single CRLF is not.

### Candidate causes

**(a) Modes task light-sleeps mid-publish: refuted for the ack.** `msg_pump()` blocks the modes task
(§1). It remains live for the event-task publishes (`/status` on the mode edge, `setup.c:827`),
which are covered today only by `s_handler_busy`. That flag is set for the MESSAGE and CONNECTED
cases and nothing else.

**(b) URCs interleaved with the prompt break `prompt1`: partly true, and it is the §2 mechanism.**
`\r\n+SQNSMQTTONPUBLISH: ...\r\n\r\n> ` parses correctly. The failure needs residue or a single
separating CRLF. The logs do not confirm or refute it.

**(c) RTS/flow-control choreography drops bytes: plausible, and the most likely trigger of §2.**
It is not an independent failure. `_queueRxBuffer()` drops a whole buffer, prompt included, if the
8-slot `_taskQueue` is full (`WalterModem.cpp:1133-1141`, `WalterModem.h:127`). `_getFreeBuffer()`
drops bytes when the 8-buffer pool is exhausted (`WalterModem.cpp:1038-1056`, `WalterModem.h:147`).
A wake-time URC burst is exactly the condition that fills both.

**(d) `net_publish_quiet_wait_ms()` interacting with an event-task publish: refuted.**
`lte_publish_quiet_wait_ms()` (`xport_lte.cpp:445-462`) is a bounded `vTaskDelay()` poll on the
modes task. It never sleeps, never touches the UART, and cannot lose a prompt.

## 3. The experiment

Extend the debug-build `sleeptest` (`modes.c:943-1182`). It already opens a real light-sleep window
and persists its report to NVS, which is the only thing that survives the dead USB port. No
RTC-memory ring and no log topic are needed. The NVS report re-prints at the next boot
(`modes_debug_sleeptest_print_saved()`, `modes.c:1105-1116`).

**Instrumentation (counters only; no behaviour change):**

1. In the library, four `extern "C"` counters exported through one accessor struct:
   - `datatx_retx`: incremented at `WalterModem.cpp:1778` when the re-transmit is for
     `WALTER_MODEM_CMD_TYPE_DATA_TX_WAIT`, together with `cmd->payloadSize` of the last one.
   - `prompt_orphan`: at the end of each `_parseRxData()` call, if `_parserData.buf` is non-NULL
     with `size == 2 && data[0]=='>' && data[1]==' '`. This is the discriminator.
   - `buf_drop_queue`: the `xQueueSend` failure at `WalterModem.cpp:1139`.
   - `buf_drop_pool`: `_getFreeBuffer()` returning NULL, `WalterModem.cpp:1052`.
2. In `xport_lte.cpp`, a 12-entry RAM ring of `{t_s, topic_tail[8], len, rc, ms}` written before and
   after each `mqttPublish()` call, where `rc` is the bool it returned and `ms` is the wall time it
   blocked.
3. In `modes.c`, print items 1 and 2 inside `modes_debug_sleeptest_report()` (`modes.c:1118-1181`),
   so they go into the NVS-saved text. Budget about 400 characters, and raise `s_st_text` from 1800
   to 2400.

**Console sequence** (debug build, USB power; the port dies during the window and returns after the
harness's own `watchdog_hard_reset()`):

```
sleeptest 6 0 0    # 0 0 = keep the build's own yield/interval
# from the host, 60 s in and again 180 s in:
relay/.venv/bin/python tools/bench/send_test_page.py <device_id> "rca1"
relay/.venv/bin/python tools/bench/send_test_page.py <device_id> "rca2"
# window closes at 360 s; +25 s grace; report saved; hard reset; report re-printed at boot
```

**Reading the outcome:**

| observation | conclusion |
|---|---|
| `prompt_orphan > 0` and `datatx_retx > 0` with the same `payloadSize` as a `/up` publish in the ring | §2 confirmed. Fix is the parser change in §4. |
| `datatx_retx > 0`, `prompt_orphan == 0`, `buf_drop_queue` or `buf_drop_pool` > 0 | The prompt buffer was dropped for want of a slot or buffer. Fix is queue and pool depth (8 to 16) plus the retry change in §4; power cost nil, about 12 kB of static RAM for the pool. |
| `datatx_retx > 0`, all three drop and orphan counters 0 | The prompt never reached the ESP32: candidate (c), seen on the wire. Next step is a logic-analyser capture on RX and RTS across one sleep boundary. No firmware change yet. |
| `datatx_retx == 0` and every publish in the ring returned true | Not reproduced in 6 minutes. Re-run at `sleeptest 30`. Two pages in six minutes reproduced it twice on release, so 30 minutes is the right second run. |

The experiment is safe on the debug build: `sleeptest` already exercises real light sleep and
already ends in a deliberate reset.

## 4. Fix direction, with power cost

Ordered by confidence. Only the first is implemented (§5).

1. **Accept the bare `> ` prompt in the parser** (`WalterModem.cpp:1466-1480`). Three lines. It is
   inert unless the orphan actually happens, and harmless if it does not: `_processModemRSP()`'s
   prompt handler refuses to act unless a `DATA_TX_WAIT` command with a payload is current
   (`WalterModem.cpp:2161`). **Power cost: zero**, since it is parser-side only with no extra AT
   traffic. It removes up to 90 s of forced-awake time per occurrence (3 × 30 s of a blocked modes
   task at about 40 mA, roughly 1.0 mAh, against about 0.025 mAh had it slept).
2. **Never re-transmit a `DATA_TX_WAIT` command line after a timeout** (`WalterModem.cpp:1775-1783`).
   Fail the command instead. This removes the security consequence rather than the cause. It is not
   implemented here because it is incomplete on its own: a modem left at the prompt owed 44 bytes
   will consume the next AT line the host sends, whichever command that is. Closing the prompt safely
   needs to know what the Sequans accepts as an abort. Not known: no Sequans AT manual is in the
   repository (`docs/reference/` holds only the display waveform table), and `0x1B` is documented
   for `AT+CMGS`, not for `AT+SQNSMQTTPUBLISH`. The cheapest check is on the bench, from the
   console: `at AT+SQNSMQTTPUBLISH=0,"pager/<device_id>/up",1,44`, then `at` alone. Watch whether the
   modem answers `OK` (the prompt timed out by itself, and how long that took) or swallows the `AT`.
   Until that is known, the recovery rule already in firmware applies: treat a `DATA_TX_WAIT` timeout
   as a dead session, call `net_session_down()` (`AT+SQNSMQTTDISCONNECT`), and reconnect. The power
   cost of a reconnect is one TLS handshake: about 5 kB, a few seconds of RRC, about 0.05 mAh.
3. **Mark publish-in-flight before the command is queued, not after** (`xport_lte.cpp:409-415`,
   `436-440`). This fixes the no-op gate described in §1. It does not fix this bug, since the modes
   task is blocked anyway. It does close the event-task publish window, which is real but not yet
   observed. Power cost: about 1.5 s of held-awake time per event-task publish, about 0.017 mAh,
   roughly 0.35 mAh/day at 20 pages/day, which is 0.3% of the 95–107 mAh/day budget.
4. **Gate `net_sleep()` itself**, immediately before `esp_light_sleep_start()` (`net.cpp:1151`), on
   connect-in-flight or publish-in-flight, returning without sleeping. `skip_sleep` is computed about
   500 lines earlier (`modes.c:1836`), and the state can change in between. The library exposes no
   public "queue non-empty" accessor (`_curCmd` and `_cmdQueue` are private, `WalterModem.h:3282,3491`),
   so "any AT command outstanding" would need a new patch. Not worth it until item 3 is measured.
   Power cost is bounded by the existing 15 s cap: at worst 15 s × 40 mA = 0.17 mAh per lost URC.
5. **Never publish from the event task.** `set_mode()` sets a `s_status_pub_pending` flag and
   `modes_run()` publishes it. This removes the whole class rather than guarding it, and removes the
   dependence on `s_handler_busy` being set by the right event cases. Power cost: no AT traffic
   change; up to one wake cycle (5 s) of extra `/status` latency, which no requirement bounds.

## 5. Change made

The parser change in §4 item 1 is applied in the vendored component at
`firmware/components/dptechnics__walter-modem/src/WalterModem.cpp:1466-1484`. Nothing else is
changed by it.
