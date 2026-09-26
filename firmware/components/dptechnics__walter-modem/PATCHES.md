# Local patches to dptechnics/walter-modem v1.5.0

This component is `dptechnics/walter-modem` v1.5.0 (upstream commit
`51b16ce0627647acc8106c2c0d93b074fd98ea88`), vendored so it can be patched. `examples/`,
`.component_hash` and `.gitignore`/`.clang-format` were dropped; everything else (`src/`,
`CMakeLists.txt`, `Kconfig`, `LICENSE`, `README.md`, `CHANGELOG.md`, `idf_component.yml`) is kept
with every file header intact, as the DPTechnics licence's redistribution terms require.

Every change below is marked `// PAGER PATCH: <number>` (in `Kconfig`, `# PAGER PATCH: <number>`)
at the spot in the source, so an upstream re-sync can find and re-apply it by grepping for that
string. `PAGER NOTE` marks a documentation correction with no code change.

## Patch 1.1 — NULL command crash (`src/WalterModem.cpp`, `_processModemRSP()`)

A URC or error line (e.g. a stray `NO CARRIER`) can reach the end of `_processModemRSP()`'s
dispatch chain (`after_processing_logic:`) with no command pending (`cmd == NULL`). The upstream
finishing condition then called `_finishModemCMD(NULL, result)` whenever `result != OK`, which
dereferences `cmd->rsp`: a crash and reboot.

**Fix**: an explicit `cmd == NULL && result != WALTER_MODEM_STATE_OK` branch ahead of that
condition frees the buffer and returns.

## Patch 1.2 — Receive bounds (`src/WalterModem.cpp`)

Three unbounded copies into or out of a `WalterModemBuffer` (`data[WALTER_MODEM_RSP_BUF_SIZE]`,
1540 bytes, `size` = valid bytes):

1. **`_addATByteToBuffer()` / `_addATBytesToBuffer()`** (UART ingest, called from
   `_parseRxData()`) wrote past `data[]` for any response, URC or payload chunk longer than the
   buffer. Both now cap at `WALTER_MODEM_RSP_BUF_SIZE - 1` (the last byte is reserved for the NUL
   that `_buffStr()` writes) and drop the excess. A payload larger than one buffer is truncated,
   not reassembled, so the command fails cleanly instead of corrupting memory.
2. **Socket receive** (`+SQNSRECV:` handler) copied `dataReceived`, a length the modem reports in
   the response header, without checking it against `cmd->payloadSize` or the bytes present in the
   buffer. The copy is now bounded by the minimum of all three, and
   `cmd->rsp->data.socketResponse.bytesReceived` reports what was actually copied.
3. **MQTT receive** (`AT+SQNSMQTTRCVMESSAGE=0,` handler) copied `cmd->payloadSize` bytes
   unconditionally and read past `buff->data` when the modem delivered fewer. The copy is now
   bounded by the bytes present in `buff` from `rspStr` onward.

## Patch 1.3 — TLS profile range (`Kconfig`)

`WALTER_MODEM_MAX_TLS_PROFILES` was `range 1 3` / `default 3`. `tlsConfigProfile()` checks
`profile_id >= WALTER_MODEM_MAX_TLS_PROFILES`, so profile 3 failed with `NO_SUCH_PROFILE` although
the Sequans modem's own `spId` range is 1-6. Raised to `range 1 6` / `default 6`.

## Patch 1.5 — CEREG lac/ci dropped for non-PSM report types (`src/WalterModem.cpp`, `_processModemRSP()`)

The `+CEREG` URC handler's `sscanf()` parses `lac`/`ci`/`act` for both extended-URC variants, but
the code copying them into the dispatched `WalterModemEvent` gated all five fields (`lac`, `ci`,
`act`, `activeTime`, `periodicTau`) behind `hasPsmInfo`, which is only true for report types 4 and
5. With `AT+CEREG=2` (`WALTER_MODEM_CEREG_REPORTS_ENABLED_WITH_LOCATION`) every event carried empty
`lac` and `ci`.

**Fix**: copy `lac`/`ci`/`act` unconditionally; `hasPsmInfo` gates only
`activeTime`/`periodicTau`.

## Patch 1.4 — SMS (`src/WalterModem.h`, `src/WalterModem.cpp`)

Adds text-mode (3GPP TS 27.005) SMS send, read, delete and new-message events, following the
library's existing patterns.

- **Event type** `WALTER_MODEM_EVENT_TYPE_SMS`: `WMSmsEventType`/`WMSmsEventData`, a
  `walterModemSmsEventHandler` typedef, a `smsHandler` union member, a `WalterModemEvent.sms`
  member, `setSmsEventHandler()` and a `_dispatchEvent()` case. It has no
  `CONFIG_WALTER_MODEM_ENABLE_SMS` guard. It fires on `+CMTI: "<mem>",<index>` (§3.4.1).
- **`smsConfig()`** runs, failing fast on the first error: `AT+CMGF=1`; `AT+CSCS="IRA"` with
  fallback to `"GSM"` (under `"GSM"` the TE charset is the GSM 03.38 alphabet and plain ASCII is
  mangled, so `"IRA"` is tried first; the result is kept in `_smsRestingCharset`); `AT+CSDH=1`
  (adds `<dcs>` to `+CMGR`); `AT+CSMP=17,167,0,0`; `AT+CNMI=2,1,0,0,0`; `AT+CPMS="ME","ME","ME"`
  (its `+CPMS:` response, §3.2.2, is captured into `rsp->data.smsStorage`). Each step is its own
  command; `_waitCmdResult()` blocks on it and returns a `bool` without the unconditional return of
  `_returnAfterReply()`, so callers can stop or clean up between steps.
- **`smsSend(number, text, useUcs2)`** uses the `DATA_TX_WAIT` pattern of `tlsWriteCredential()`:
  wait for the `"> "` prompt, then write `text` followed by Ctrl-Z (0x1A, §4.3). `text` is capped
  at 280 bytes. With `useUcs2`, `AT+CSCS` and `AT+CSMP` are switched to UCS2 / DCS 8 for the send
  and always restored to the resting values afterwards; otherwise neither is touched.
- **`smsRead(index)`**: `AT+CMGR=<index>`. The response is two lines (`+CMGR: <stat>,<oa>,
  [<alpha>],<scts>[,...]`, then the body). `_processModemRSP()` keeps `_smsCmgrAwaitingBody` and
  checks it first, so a body that starts with `OK` or `ERROR` is not taken for a result line;
  `_smsSplitCmgrFields()` splits the quoted-CSV header. The result is `rsp->data.smsRead`, with
  `dcs` (field 7, -1 when the CSDH fields are absent). A body containing an embedded CRLF is cut at
  the first line.
- **`smsDelete(index)`**: `AT+CMGD=<index>`, single-line `OK`.
- **`+CMS ERROR:`** (§3.2.5) fails the in-flight command with `WALTER_MODEM_STATE_ERROR`, like
  `+CME ERROR:`.

The AT sequences are taken from 3GPP TS 27.005, not from a Sequans GM02SP datasheet. The `rsp`/`cb`
parameters are kept for API consistency; the async branch of `_waitCmdResult()` is unused.

## 1.6 Explicit socket ids (`src/proto/WalterSocket.cpp`, `_socketGet()`)

`WalterModemSocket::id` defaults to `1` for every entry of `_socketSet` and only
`_socketReserve()` assigns a real id, so a lookup for any explicit id other than 1 returned NULL
and `socketConfig(4)` failed locally with `NO_FREE_SOCKET`. `_socketGet(id)` now maps id N onto
slot N-1 (the modem's connection ids are 1..6) and claims it.

## 1.7 Stray line break before a final `OK` (`src/WalterModem.cpp`, response pre-processing)

With `AT+SQNSRECV` on a TLS socket, a payload ending in a bare `\n` (a PEM file) leaves the final
result queued as `\n\r\nOK\r\n`. It never matches the expected `OK`, so the command times out
after 30 s although the data arrived in 100 ms. A buffer that is only CR/LF characters followed by
exactly `OK` is now normalised to `OK`.

Not fixed: the one-byte miscount in the payload parser that causes this. The last payload byte
(the `\n`) is lost from such a read. Patch 1.22 exposes it as `bytesReceived < bytesClaimed`; if a
hash mismatch is seen on a body ending in `\n`, look here first.

## 1.8 Restricted SIM access (`+CRSM`) (`src/WalterModem.cpp`, `src/WalterModem.h`)

The library has no `AT+CRSM` support and `sendCmd()` returns no response text. The response
processor now keeps the last `+CRSM: <sw1>,<sw2>[,<hex>]` line, readable through
`WalterModem::simLastCRSM()`. The pager uses it to read `EF_GID1`, recognise an MVNO SIM and pick
its APN.

## 1.9 Serving-cell MNC digit width (`src/WalterModem.cpp`, `src/WalterModem.h`)

The `Nc:` field of `+SQNMONI` was parsed with `strToUint8()` into a `uint8_t`, which loses the
digit width (`"05"` and `"5"` are both 5) and cannot hold a 3-digit MNC such as 410 (3-digit MNCs
run 0-999). `strToUint8()` failed on the range check and left `nc` at 0 while the surrounding code
set the digit count from the field's text width regardless, so a failed parse looked like a
genuine `"000"`.

**Fix**: `WalterModemCellInformation.nc` is a `uint16_t` parsed with `strToUint16()`, and a new
`ncDigits` field (2 or 3, set from the text width) is set only when the numeric parse succeeded; 0
means unknown.

## 1.10 Bounded, watchdog-feeding synchronous command wait (`src/WalterDefines.h`, `src/WalterModem.cpp`)

The command queue is single-threaded, and a slow command holds it for the retry logic's full
`3 * CONFIG_WALTER_MODEM_CMD_TIMEOUT_MS` (90 s at 30 s), which is past a 60 s task watchdog. The
three synchronous wait sites waited on their condition variable with no timeout, so nothing fed
either watchdog while a command was stuck (e.g. a slow `AT+COPS=0` during a network search).

**Fix**: all three untimed `cond.wait(lock, pred)` sites loop on
`cond.wait_for(lock, std::chrono::milliseconds(1000), pred)` and call
`walter_modem_block_tick()` every second the predicate is still false. The sites are the
`_returnAfterReply()` macro in `WalterDefines.h`, `_waitCmdResult()` and `getNetworkRegState()`
in `WalterModem.cpp`; each keeps its own predicate.

`walter_modem_block_tick()` is `extern "C"` in `WalterDefines.h`, with a weak no-op default in
`WalterModem.cpp` so the component links standalone. The application overrides it to feed its
watchdogs for a bounded time per stage.

## 1.11 Orphaned `> ` data prompt (`src/WalterModem.cpp`, `_parseRxData()`)

`_parseRxData()` recognised the data prompt only as `prompt1`: `buf->size >= 4 && buf->data[2] ==
'>' && buf->data[3] == ' '`, i.e. with the prompt's own leading CRLF at `data[0..1]`. The CRLF
check above it queues the buffer when it finds a CRLF anywhere past offset 2. So if the parser
buffer held residue when the prompt's `\r\n` arrived, that CRLF ended the residue, and the
remaining `> ` started a new buffer of size 2, too short for both checks. The prompt was never
queued, the `DATA_TX_WAIT` payload was never written, and the command timed out with the modem
still owed `payloadSize` bytes. `_processModemCMD()` then re-transmitted the AT command line,
which the modem consumed as the payload and published as the message body. The residue comes from
bytes truncated at a light-sleep boundary.

**Fix**: a `prompt3` case for the bare, stripped prompt (`size == 2 && data[0] == '>' &&
data[1] == ' '`). It is safe because the prompt handler in `_processModemRSP()` acts only when the
current command is a `DATA_TX_WAIT` with a payload.

## 1.12 DATA_TX_WAIT timeout sends the payload instead of the AT command line, plus instrumentation counters (`src/WalterModem.cpp`, `src/WalterModem.h`, `src/WalterDefines.h`)

After answering the `> ` prompt the Sequans modem has no timeout of its own: it keeps waiting for
the promised payload bytes. When `_processModemCMD()` timed out and re-transmitted the AT command
line, the modem consumed the first bytes of that line as the payload (`AT+SQ` acked as a 5-byte
publish with `+SQNSMQTTPUBLISH: 5`) and answered the rest with `+CME ERROR: 4`.

**Fix**: in the `DATA_TX_WAIT`/`TX_WAIT` retry branch, on a timeout (not a `RETRY_AFTER_ERROR`
resend) of a `DATA_TX_WAIT` command that has a payload, the first timeout writes
`cmd->payload`/`cmd->payloadSize` instead of the AT command line and keeps waiting for `OK`. If
the modem is still at the prompt this completes the original publish; if not, the bytes draw a
`+CME ERROR` or a timeout that the existing retry path handles. A second timeout (tracked by the
new `WalterModemCmd::dataTxPayloadRetried`, reset in `_queueModemCMD()`) fails the command with
`WALTER_MODEM_STATE_TIMEOUT` instead of retrying blind; the application should treat that as a
dead session. A command now takes at most two attempts before failing.

**Counters** (free-running, never reset, increments only), declared in `WalterDefines.h` as
`walter_modem_pager_counters_t` and read through `walter_modem_pager_counters()`:

- `datatx_retx`: payload-instead-of-command-line writes (above).
- `prompt_orphan`: bare `"> "` prompts matched by patch 1.11's `prompt3`.
- `buf_drop_queue`: parsed buffers dropped because the 8-slot `_taskQueue` was full.
- `buf_drop_pool`: allocation failures with the 8-buffer pool exhausted.

## 1.13 Per-command timeout for MQTT publish/subscribe/disconnect/config, plus stall-attribution counters (`src/WalterModem.cpp`, `src/WalterModem.h`, `src/WalterDefines.h`, `src/proto/WalterMQTT.cpp`)

Every AT command shared one timeout and attempt count (30 s x 3 = 90 s worst case per stall), so
two such stalls in one watchdog stage exceeded the application's watchdog budget, and nothing told
the stalled commands apart.

**Per-command timeout.** `WalterModemCmd` gains `timeoutTicks` (0 = the library's own
`WALTER_MODEM_CMD_TIMEOUT_TICKS`, resolved in `_queueModemCMD()`'s translation unit because the
macro is private to `WalterModem.cpp`). `_queueModemCMD()` gains an optional last parameter
`cmdTimeoutTicks`. `_processModemCMD()`'s `TX_WAIT`/`DATA_TX_WAIT`/`WAIT` deadline checks read
`cmd->timeoutTicks`. `mqttPublish()`, `mqttSubscribe()`, `mqttDisconnect()` and `mqttConfig()`
(`src/proto/WalterMQTT.cpp`) pass 10 s x 2 attempts (`PAGER_MQTT_CMD_TIMEOUT_TICKS`,
`PAGER_MQTT_CMD_ATTEMPTS`). `mqttConnect()` keeps the default. Every other caller is unchanged.

**Stall attribution.** More fields in `walter_modem_pager_counters_t`:

- `prompt_handled`: payload writes run by the `"> "` prompt handler.
- `payload_bytes_written`: bytes written by the prompt handler and by patch 1.12's write (the
  `uart_write_bytes()` return value, previously discarded).
- `txdone_timeouts`: `uart_wait_tx_done()` 10 ms waits in `_uartWrite()` that did not return
  `ESP_OK` (10 ms at 115200 baud is about 115 bytes, so a longer line can return unflushed).
- `stall_cmd` (first 24 characters of the AT line), `stall_elapsed_ms`, `stall_cts_level`
  (`gpio_get_level()` on CTS) and `stall_tx_ring_bytes`: a snapshot taken by the new private
  `_pagerSnapshotStall()` whenever `_processModemCMD()` re-evaluates a pending command that has
  run for >= 5 s; it keeps the most recent one. `stall_tx_ring_bytes` always reads 0 because the
  UART is installed with a 0-byte TX ring; `txdone_timeouts` is the wire-level discriminator.

## 1.14 `checkComm()` takes a per-command attempt/timeout budget (`src/WalterModem.cpp`, `src/WalterModem.h`)

`checkComm()` sends a bare `AT` with the library default of 3 x 30 s. A fire-and-forget probe that
went unanswered held the single in-flight command slot (`_curCmd`) for 30 s per attempt, and every
later command queued behind it.

**Fix**: `checkComm()` gains the optional trailing parameters `uint8_t maxAttempts =
WALTER_MODEM_DEFAULT_CMD_ATTEMPTS` and `TickType_t cmdTimeoutTicks = 0` (library default), passed
through to `_runCmd`. Existing callers are unchanged. A short budget loses nothing: the modem
releases its queued URCs when it accepts a command, not when the host sees the answer.

## 1.15 Stall-mechanism discriminator counters (`src/WalterModem.cpp`, `src/WalterModem.h`, `src/WalterDefines.h`)

Light sleep kills the USB console, so there is no in-window AT trace of a 30 s command stall. The
wire is ruled out (`txdone_timeouts`, `buf_drop_*` stay 0), which leaves three explanations: a
response completes the wrong command inside a URC flush burst, `_receivingPayload` sticks true so
every byte is consumed as payload, or the modem did not answer. Two counters tell them apart:

- `rsp_no_cmd`: incremented in `_processModemRSP()`'s `RSP_PROC_FINISH` region when a buffer
  reaches the completion test with `cmd == NULL` (and so `result == OK`, patch 1.1) and is freed
  unused. The desync explanation predicts at least one per stall, the others none.
- `payload_stuck_ms`: set in `_processModemCMD()`'s timeout paths (`TX_WAIT`/`DATA_TX_WAIT` and
  `WAIT`) to how long `_receivingPayload` had been true, using the new `_receivingPayloadSetAt`
  tick stamp set in `_parseRxData()`. The sticky-payload explanation predicts a value above 0.
  It keeps the most recent observation.

## 1.16 A failed hardware `reset()` costs ~91 s: the discarded `+SYSSTART` banner is remembered, and the wait budget matches the datasheet (`src/WalterModem.cpp`, `src/WalterModem.h`)

`reset()` pulses the reset pin, waits 1000 ms, then queues a `TX_WAIT` for `"+SYSSTART"` with the
library default (3 x 30 s). `_parseRxData()` discards every byte received while `_hardwareReset`
is true, which includes that 1000 ms window. If the modem's boot banner lands in it, the wait never
sees the banner and burns the full 90 s (1 s + 90 s = ~91 s).

**(a) Remember the banner.** `_parseRxData()` sets the new `_sawSysStartDuringReset` flag
(`WalterModem.h`, next to `_hardwareReset`) when the bytes it discards contain `"+SYSSTART"`
(`memmem()`). `reset()` clears the flag at its start. `_processModemCMD()`'s first evaluation of a
`TX_WAIT`/`DATA_TX_WAIT` command whose `atRsp` is literally `"+SYSSTART"` consumes it and completes
the command with `WALTER_MODEM_STATE_OK`. It is scoped to that string because `softReset()` queues
the same wait without setting `_hardwareReset`, so the flag is never true there. A `"+SYSSTART"`
split across two UART reads at the discard boundary is still missed.

**(b) A 10 s, 1-attempt budget** for the wait (`PAGER_RESET_SYSSTART_TIMEOUT_TICKS`,
`PAGER_RESET_SYSSTART_ATTEMPTS`), passed from `reset()` through the 1.13/1.14 parameters. The
GM02SP boots in ~2-3 s. It bounds whatever (a) misses to 10 s, and callers already handle a false
return from `reset()`.

## 1.17 Application-installable UART/response trace hook (`src/WalterModem.h`, `src/WalterModem.cpp`, `src/WalterDefines.h`)

A single null-checked function pointer, `walter_pager_trace_fn` (`void (*)(char kind, const
uint8_t *data, size_t len)`, declared before `class WalterModem`), installed with
`WalterModem::setPagerTraceHook(fn)` (NULL clears it) into the file-static `s_pagerTraceHook`. It
lets a debug-build flight recorder see the library's UART traffic without duplicating its RX/TX
and response-pairing logic. It is called, if non-NULL, from three sites and changes no behaviour:

- `'R'`: `_handleRxData(void*)` (the ESP-IDF variant only), right after `_uartRead()` returns, only
  when `uartBufLen > 0`, before `_parseRxData()` sees the bytes.
- `'T'`: the `_transmitCmd` macro's ESP-IDF branch (`WalterDefines.h`), once per non-NULL
  `atCmd[i]` element before its `uart_write_bytes()`. The trailing `"\r\n"`/`"\n"` is not traced.
- `'U'`: `_processModemRSP()`'s `RSP_PROC_FINISH` region, at the `rsp_no_cmd` increment of patch
  1.15: the same unpaired buffer.

`data` is valid only for the duration of the call. The hook is not `#ifdef`-gated in this
component.

## 1.18 A stray leading 0xFF at the start of a wake's first message (`src/WalterModem.cpp`, `src/WalterDefines.h`)

On every observed wake from light sleep the modem UART delivers one stray `0xFF` byte, in the same
millisecond as the wake path's UART reconfiguration, before anything else. `_parseRxData()` assumes
each message starts with `"\r\n"`, so the `0xFF` was glued onto the next line: `"\xff\r\nOK\r\n"`
(the probe's `OK` not credited, the probe fell through to its 15 s wait) or
`"\xff\r\n+SQNSMQTTONMESSAGE:0,...\r\n"` (the page URC not recognised, the page lost).

**Fix**: at the top of `_parseRxData()`'s per-message loop, before the known-size payload branch,
if `!_receivingPayload`, the parser buffer is empty (`_parserData.buf == NULL ||
_parserData.buf->size == 0`) and the first byte of the message is `0xFF`, the byte is dropped
(`offset += 1; continue;`) and counted in `glitch_dropped`
(`walter_modem_pager_counters_t`). A real response or URC always starts a fresh buffer with
`"\r\n"`, so this cannot match a byte inside a binary payload.

## 1.19 A finished command left in `_curCmd` is re-finished by a stray post-wake response and wedges the command slot (`src/WalterModem.cpp`, `src/WalterDefines.h`)

`_finishModemCMD()` moves a command to `WALTER_MODEM_CMD_STATE_SYNC_LOCK_NOTIFIED` and notifies the
caller; the blocking caller (`_returnAfterReply()`) wakes, reads the result and only then sets the
state to `COMPLETE`. Until `_cmdProcessingTask` clears `_curCmd` on its next iteration the finished
command is still `_curCmd`, and FreeRTOS ticks stop during light sleep, so this window can span a
whole sleep-and-wake cycle. A stray response arriving then (e.g. `+CME ERROR: 4` drawn by the wake
bytes' `"\r\n"`) was paired with the finished command, and the error handler called
`_finishModemCMD()` on it a second time. That set its state back to `SYNC_LOCK_NOTIFIED` and
notified a condition variable nobody waits on, so `_cmdProcessingTask`'s `SYNC_LOCK_NOTIFIED` case
waited forever. `_curCmd = qItem.cmd` only runs when `_curCmd == NULL`, so every later command
queued behind a slot that never empties, and the task watchdog eventually fired.

**Fix**: at the response-pairing site in `_cmdProcessingTask` (the `qItem.rsp != NULL` branch), a
response is paired with `_curCmd` only when `_curCmd != NULL && _curCmd->state ==
WALTER_MODEM_CMD_STATE_PENDING`. In any other state it is paired with `NULL`, a path that already
works (patch 1.1 drops an error line with no command; a stray `OK` is freed unused and counted by
`rsp_no_cmd`).

**Counter**: `rsp_stale_cmd` (`walter_modem_pager_counters_t`) counts the responses paired with
`NULL` because `_curCmd` was non-NULL but not `PENDING`.

## 1.20 Optional per-call attempt/timeout budget on `getVoltage()`, `getRSSI()`, `sendCmd()` and `mqttReceive()` (`src/WalterModem.cpp`, `src/WalterModem.h`, `src/proto/WalterMQTT.cpp`)

Without a budget a slow (not wedged) reply to any of these costs the library default of 3 x 30 s,
and two such waits inside one watchdog stage exceed the application's budget even with 1.19.

**Fix**: `getVoltage()`, `getRSSI()` and `sendCmd()` each gain the optional trailing parameters
`uint8_t maxAttempts = WALTER_MODEM_DEFAULT_CMD_ATTEMPTS, TickType_t cmdTimeoutTicks = 0`, passed
through to `_runCmd`/`_queueModemCMD` as in 1.13/1.14. `mqttReceive()` gains the same parameters on
both its qos-0 and qos>0 branches. Callers that do not pass them keep the exact 30 s x 3 default.

## 1.21 `mktime()` is TZ-dependent; the library's own UTC-epoch math assumed it was not (`src/WalterModem.cpp`)

`strTotime()` (the GNSS-fix timestamp parser) and the `AT+CCLK?` response handler built a `struct
tm` from a value known to be UTC (for CCLK, re-based to UTC by the handler's own `tz_offset`
arithmetic) and called `mktime()`, assuming it behaves like `timegm()`. `mktime()` converts using
the process's current `TZ`, so it is correct only while the application never calls
`setenv("TZ", ...)`. Once the application set a Pacific `TZ`, a CCLK-seeded epoch came out 8 hours
ahead of the true time.

**Fix**: a TZ-independent UTC calendar-to-epoch conversion, `_pagerTimegm()` (the public-domain
"days from civil" algorithm), added near `strTotime()` and used at both call sites. `timegm()` is
not used because this toolchain's newlib `<time.h>` does not declare it. Pushing and popping `TZ`
around `mktime()` was rejected because `tzset()` is not thread-safe and this file's tasks are not
necessarily the application's TZ-owning task.
