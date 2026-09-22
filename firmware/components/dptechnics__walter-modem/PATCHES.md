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
