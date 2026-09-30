"""Wire-format validation for the MQTT envelope (docs/PROTOCOL.md §3): validate, never strip.
Applied to device payloads on `/up` and `/status`; the HTTP API strips before a message exists."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal


from app import wirecbor

logger = logging.getLogger("relay.wire")


# docs/PROTOCOL.md §1: ids are opaque; one generic shape for all id kinds.
ID_RE = re.compile(r"^[a-z0-9_]{3,16}$")
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,23}$")
SESSION_RE = re.compile(r"^s_[0-9a-f]{8}$")
# U+0000-001F and U+007F.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# docs/PROTOCOL.md §3.1: `from`/`to` carry an alias. `system` also matches this regex; reserving
# it is a store-layer rule, not a wire-shape rule.
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
SYSTEM_ALIAS = "system"
ACK_VALUES = {"shown", "read"}

# Firmware main-loop stage table that `/status`'s `stage` indexes into; used only for logging.
STAGE_NAMES: tuple[str, ...] = (
    "?",
    "boot",
    "network init",
    "loop top",
    "entering light sleep",
    "just woke from light sleep",
    "input/ui",
    "render",
    "mqtt status/retry",
    "message pump",
    "modem health check",
    "location",
    "sms",
    "ca trust",
    "saving state",
    "deliberate restart",
)


def stage_name(stage: int | None) -> str | None:
    """`STAGE_NAMES[stage]`, or the raw int as a string when out of range (never raises)."""
    if stage is None:
        return None
    if 0 <= stage < len(STAGE_NAMES):
        return STAGE_NAMES[stage]
    return str(stage)


def is_valid_alias(value: str) -> bool:
    """docs/PROTOCOL.md §3.1: the alias regex, or the literal `system`."""
    return value == SYSTEM_ALIAS or bool(ALIAS_RE.match(value))


def strip_control_chars(text: str) -> str:
    """HTTP ingest path only (docs/PROTOCOL.md §3.1: the relay strips parent API bodies)."""
    return CONTROL_CHAR_RE.sub("", text)


def validate_body(body: str) -> None:
    """Raises ValueError if `body` violates §3.1; never strips (docs/PROTOCOL.md §3.4)."""
    if not body:
        raise ValueError("body must not be empty")
    if CONTROL_CHAR_RE.search(body):
        raise ValueError("body contains control characters")
    if len(body.encode("utf-8")) > BODY_MAX_UTF8_BYTES:


def validate_ts(ts: int) -> None:
    if ts == 0:
        return
    if not (1_000_000_000 <= ts <= 2_000_000_000):
        raise ValueError("ts out of range")


def resolve_ts(ts: int) -> int:
    """§3.5: ts=0 means "no network time yet"; the relay substitutes its receive time."""
    return ts if ts != 0 else int(time.time())


#: `n` is a 52-bit counter; `2**53` is the exact bound (docs/PROTOCOL.md §3.1, §14.2).
N_MAX_EXCLUSIVE = 2**53


def _check_n_range(value: int | None) -> int | None:
    """§14.2: `n` is a replay counter on every signed envelope (`/up`, `/status`, `/loc`)."""
    if value is not None and not (0 <= value < N_MAX_EXCLUSIVE):
        raise ValueError("n out of range")
    return value


class UpEnvelope(BaseModel):
    """A payload on `pager/{device_id}/up`: an ack (§3.2) or a device up-message."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    id: str
    ts: int
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    body: str | None = None
    ack: str | None = None
    # §3.2: only the default kind (an ack or a content message, `kind` omitted) is handled here;
    # any other kind takes the unknown-kind path (§3.4).
    kind: str | None = None
    # §14.2: present on every signed envelope; absent when `authMode` is "password".
    n: int | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not ID_RE.match(value):
            raise ValueError("invalid id format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int) -> int:
        validate_ts(value)
        return value

    @field_validator("n")
    @classmethod
    def _check_n(cls, value: int | None) -> int | None:
        return _check_n_range(value)

    @field_validator("to")
    @classmethod
    def _check_to(cls, value: str | None) -> str | None:
        # §3.1: same alias shape as `from`; whether `to:"system"` is routable is a store decision.
        if value is not None and not is_valid_alias(value):
            raise ValueError("invalid 'to' alias format")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> UpEnvelope:
        if self.kind is not None:
            # §3.4: an unrecognised kind is not rendered or acked, only counted and dropped.
            raise ValueError(f"unknown kind on /up: {self.kind!r}")
        if self.ack is not None:
            if self.ack not in ACK_VALUES:
                raise ValueError("invalid ack value")
            if self.body:
                # §3.2: non-null ack + non-empty body is malformed.
                raise ValueError("ack payload must not carry a body")
            if self.to is not None:
                # §3.1: `to` is restricted to up content messages; an ack
                # carrying `to` is malformed.
                raise ValueError("ack payload must not carry a 'to'")
        else:
            if self.from_ is None or not is_valid_alias(self.from_):
                raise ValueError("content message requires a valid 'from'")
            if not self.body:
                raise ValueError("content message requires a non-empty body")
            validate_body(self.body)
        return self

    @property
    def is_ack(self) -> bool:
        return self.ack is not None


class StatusEnvelope(BaseModel):
    """A payload on `pager/{device_id}/status` (device publish or broker LWT), §5.1/§5.2."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    state: Literal["online", "offline"]
    mode: str | None = None
    batt_mv: int | None = None
    rssi: int | None = None
    session: str
    ts: int | None = None
    fw: str | None = None
    # §4.5/§5.1: display only; stored as reported, never written back.
    loc_period_s: int | None = None
    loc_min_s: int | None = None
    # CA trust state (optional): trust state and the first 16 hex chars of the pinned CA's SHA-256.
    ca_fp: str | None = None
    # Seconds until the device's own backoff next allows an attempt, 0 = now (optional); the bound
    # is generous so a backoff retune is not rejected.
    loc_backoff_s: int | None = None
    # Count of sms_log audit entries dropped for lack of NVS queue space, normally 0 (optional).
    sms_lost: int | None = None
    # MQTT-session generation within a boot, bumped on each silent modem session resume; a change
    # triggers the online-edge republish, since a resume drops QoS 1 `/down` in the <=10 s gap.
    link: int | None = None
    # Crash diagnostics (optional, display only): `rst` is the last esp_reset_reason_t, `stage`
    # indexes `STAGE_NAMES`, `abn` counts abnormal resets since power-on.
    rst: int | None = None
    stage: int | None = None
    abn: int | None = None
    # Crash diagnostics (optional, display only): the AT command name the main loop was stuck on.
    stallcmd: str | None = None
    # Book-pull capability gate: only `1` is legal; anything else is dropped to `None`, not
    # rejected (§3.4 "unknown values").
    bpull: int | None = None
    # GNSS-while-moving interval in seconds, 0 = off (display only; docs/PROTOCOL.md §13.3).
    loc_move_s: int | None = None
    # §14.2: present on every signed envelope; absent on the unsigned LWT (§14.6) and "password".
    n: int | None = None

    @field_validator("loc_period_s", "loc_min_s", "loc_backoff_s", "loc_move_s")
    @classmethod
    def _check_loc_timing(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value <= 86400):
            raise ValueError("loc_period_s/loc_min_s/loc_backoff_s/loc_move_s out of range")
        return value

    @field_validator("ca_fp")
    @classmethod
    def _check_ca_fp(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{16}", value):
            raise ValueError("ca_fp must be 16 lowercase hex chars")
        return value

    @field_validator("sms_lost")
    @classmethod
    def _check_sms_lost(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("sms_lost must be >= 0")
        return value

    @field_validator("link")
    @classmethod
    def _check_link(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("link must be >= 0")
        return value

    @field_validator("rst", "stage")
    @classmethod
    def _check_rst_stage(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value <= 255):
            raise ValueError("rst/stage out of range")
        return value

    @field_validator("abn")
    @classmethod
    def _check_abn(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value <= 65535):
            raise ValueError("abn out of range")
        return value

    @field_validator("bpull")
    @classmethod
    def _check_bpull(cls, value: int | None) -> int | None:
        return value if value == 1 else None

    @field_validator("stallcmd")
    @classmethod
    def _check_stallcmd(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 24 or not value.isprintable() or not value.isascii()):
            raise ValueError("stallcmd must be <= 24 printable ASCII chars")
        return value

    @field_validator("n")
    @classmethod
    def _check_n(cls, value: int | None) -> int | None:
        return _check_n_range(value)

    @field_validator("session")
    @classmethod
    def _check_session(cls, value: str) -> str:
        if not SESSION_RE.match(value):
            raise ValueError("invalid session format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int | None) -> int | None:
        if value is not None:
            validate_ts(value)
        return value

    @field_validator("batt_mv")
    @classmethod
    def _check_batt(cls, value: int | None) -> int | None:
            raise ValueError("batt_mv out of range")
        return value

    @field_validator("rssi")
    @classmethod
    def _check_rssi(cls, value: int | None) -> int | None:
        if value is not None and not (-140 <= value <= 0):
            raise ValueError("rssi out of range")
        return value

    @model_validator(mode="after")
    def _check_online_fields(self) -> StatusEnvelope:
        if self.state == "online" and (
        ):
            raise ValueError("online status missing a required field")
        return self


class DownEnvelope(BaseModel):
    """A payload on `pager/{device_id}/down` (§3.1/§3.2): `loc_req` has no `body`, `ack` is null."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    id: str
    ts: int
    kind: Literal["msg", "loc_req"] = "msg"
    from_: str | None = Field(default=None, alias="from")
    body: str | None = None
    # The author's alias on a group `msg`, where `from` names the group; absent on one-to-one
    # pages and on `loc_req` (docs/PROTOCOL.md §3.1).
    sndr: str | None = None
    ack: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not ID_RE.match(value):
            raise ValueError("invalid id format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int) -> int:
        validate_ts(value)
        return value

    @field_validator("from_")
    @classmethod
    def _check_from(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_alias(value):
            raise ValueError("invalid 'from' alias format")
        return value

    @field_validator("sndr")
    @classmethod
    def _check_sndr(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_alias(value):
            raise ValueError("invalid 'sndr' alias format")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> DownEnvelope:
        if self.ack is not None:
            # §3.2: every down envelope has ack:null.
            raise ValueError("down envelope must have ack:null")
        # §3.1: `from` is required on every down envelope; acks are up-only.
        if self.from_ is None or not is_valid_alias(self.from_):
            raise ValueError("down envelope requires a valid 'from'")
        if self.kind == "loc_req":
            if self.body:
                # §3.2: a loc_req has no body.
                raise ValueError("loc_req down envelope must not carry a body")
            if self.sndr is not None:
                # `sndr` is only ever on a `msg`.
                raise ValueError("loc_req down envelope must not carry 'sndr'")
        else:
            if not self.body:
                raise ValueError("msg down envelope requires a non-empty body")
            validate_body(self.body)
        return self


def parse_envelope_bytes(raw: bytes) -> dict[str, Any] | None:
    """Size/UTF-8/JSON-object check per §3.3/§3.4. Returns the parsed JSON
    object, or None if the payload is malformed at this coarse level."""
    if len(raw) > MAX_ENVELOPE_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


EnvelopeEncoding = Literal["json", "cbor"]


def is_oversize(raw: bytes) -> bool:
    """§3.3/§14.4 step 1: the 640-byte check, done *before* anything about
    the payload (encoding, signature, shape) is inspected."""
    return len(raw) > MAX_ENVELOPE_BYTES


def decode_envelope_bytes(raw: bytes) -> tuple[dict[str, Any], EnvelopeEncoding] | None:
    """§14.4 step 4: CBOR (first-byte dispatch) or JSON decode; `(dict, encoding)` or None."""
    if is_oversize(raw):
        return None
    if wirecbor.is_cbor(raw):
        try:
            data = wirecbor.decode(raw)
        except Exception:  # noqa: BLE001 -- cbor2 raises several distinct exception types
            # An unknown integer key is dropped by wirecbor.translate_to_names (§3.1), not raised.
            return None
        if not isinstance(data, dict):
            return None
        return data, "cbor"
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data, "json"


# §14.6: the one unsigned inbound envelope an hmac device may send, the broker LWT: exactly
# {v, state:"offline", session}, matching §5.2's wire example.
_LWT_ALLOWED_KEYS = {"v", "state", "session"}


def is_unsigned_lwt_shape(data: dict[str, Any]) -> bool:
    if not set(data.keys()) <= _LWT_ALLOWED_KEYS:
        return False
    return data.get("state") == "offline" and isinstance(data.get("session"), str)


def log_malformed(topic: str, raw: bytes, reason: str) -> None:
    """§3.4: relay logs the topic and first 64 bytes, then drops."""
    logger.warning("malformed payload on %s (%s): %r", topic, reason, raw[:64])


def build_down_payload(
    *,
    msg_id: str,
    ts: int,
    body: str | None = None,
    v: int = 1,
    kind: Literal["msg", "loc_req"] = "msg",
    from_: str = "parent",
    sndr: str | None = None,
) -> bytes:
    """Minified UTF-8 JSON in §3.1 field order; `kind` is omitted for `msg`, `sndr` when absent."""
    if kind != "loc_req" and not body:
        raise ValueError("msg down payload requires a non-empty body")
    if kind == "loc_req" and sndr is not None:
        raise ValueError("loc_req down payload must not carry 'sndr'")
    obj: dict[str, Any] = {"v": v, "id": msg_id, "ts": ts}
    if kind != "msg":
        obj["kind"] = kind
    obj["from"] = from_
    if sndr is not None:
        obj["sndr"] = sndr
    if kind != "loc_req":
        obj["body"] = body
    obj["ack"] = None
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class LocFix(BaseModel):
    """`loc` object inside a `/loc` envelope, per §13.2."""

    model_config = ConfigDict(extra="ignore")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    acc: int | None = Field(default=None, ge=0)
    fix_ts: int
    src: Literal["gnss", "cell"] = "gnss"

    @field_validator("fix_ts")
    @classmethod
    def _check_fix_ts(cls, value: int) -> int:
        # §13.2: `fix_ts` is the fix's own epoch time, not the envelope `ts`; 0 is rejected, not
        # substituted.
        if value == 0:
            raise ValueError("loc.fix_ts must not be 0")
        validate_ts(value)
        return value


# Cell sub-keys mcc=0, mnc=1, tac=2, ci=3, rsrp=4 (app/wirecbor.py's CELL_KEYMAP); `mcc`/`mnc` are
# strings so a leading zero ("05") survives.
_MCC_RE = re.compile(r"^\d{3}$")
_MNC_RE = re.compile(r"^\d{2,3}$")

# Known `why` values for an unsolicited `/loc` (§13.2); anything else is stored as absent, never
# rejected.
_WHY_VALUES = {"still", "cell", "move", "gnss", "stop"}
CELL_TAC_MAX = 65535
CELL_CI_MAX = 268_435_455  # 2**28 - 1: E-UTRAN cell identity is 28 bits.
CELL_RSRP_MIN = -156
CELL_RSRP_MAX = -30


class CellInfo(BaseModel):
    """`cell` sub-map on a `/loc` (§13.2): the serving cell, sent with `no_fix` or a fix."""

    model_config = ConfigDict(extra="ignore")

    mcc: str
    mnc: str
    tac: int = Field(ge=0, le=CELL_TAC_MAX)
    ci: int = Field(ge=0, le=CELL_CI_MAX)
    rsrp: int | None = Field(default=None, ge=CELL_RSRP_MIN, le=CELL_RSRP_MAX)

    @field_validator("mcc")
    @classmethod
    def _check_mcc(cls, value: str) -> str:
        if not _MCC_RE.match(value):
            raise ValueError("cell.mcc must be exactly 3 digits")
        return value

    @field_validator("mnc")
    @classmethod
    def _check_mnc(cls, value: str) -> str:
        if not _MNC_RE.match(value):
            raise ValueError("cell.mnc must be 2 or 3 digits")
        return value


class LocEnvelope(BaseModel):
    """A payload on `pager/{device_id}/loc` (§13.2); `loc` and `req` are required keys, nullable."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    id: str
    ts: int
    loc: LocFix | None
    req: str | None
    cached: bool = False
    err: Literal["no_fix", "disabled"] | None = None
    # Optional serving cell; a malformed one is dropped by `_drop_malformed_cell`, not rejected.
    cell: CellInfo | None = None
    # Why an unsolicited (`req:null`) `/loc` was sent; unknown values become `None` (§13.2: stored
    # as absent, never a reason to drop the envelope).
    why: str | None = None
    # §14.2: present on every signed envelope.
    n: int | None = None

    @field_validator("why", mode="before")
    @classmethod
    def _check_why(cls, value: Any) -> str | None:
        if isinstance(value, str) and value in _WHY_VALUES:
            return value
        return None

    @model_validator(mode="before")
    @classmethod
    def _drop_malformed_cell(cls, data: Any) -> Any:
        """§13.2: a malformed `cell` is treated as absent; the fix/no_fix answer is still used."""
        if not isinstance(data, dict) or "cell" not in data:
            return data
        cell = data["cell"]
        if cell is None:
            return data
        try:
            CellInfo.model_validate(cell)
        except Exception:  # noqa: BLE001 -- any validation error means "treat as absent"
            data = dict(data)
            data.pop("cell", None)
        return data

    @field_validator("n")
    @classmethod
    def _check_n(cls, value: int | None) -> int | None:
        return _check_n_range(value)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not ID_RE.match(value):
            raise ValueError("invalid id format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int) -> int:
        validate_ts(value)
        return value

    @field_validator("req")
    @classmethod
    def _check_req(cls, value: str | None) -> str | None:
        if value is not None and not ID_RE.match(value):
            raise ValueError("invalid 'req' id format")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> LocEnvelope:
        # §13.2: `loc` is null only when `err` is set, and vice versa.
        if self.loc is None and self.err is None:
            raise ValueError("loc:null requires err to be set")
        if self.loc is not None and self.err is not None:
            raise ValueError("loc and err are mutually exclusive")
        return self


# `peer` is always a real E.164 number, never an alias reference; same shape as `app/ingest.py`'s
_SMS_PEER_RE = re.compile(r"^\+[1-9]\d{6,14}$")
SMS_LOG_DIR_VALUES = ("out", "in")
SMS_LOG_ST_VALUES = ("sent", "failed", "recv", "blocked")
# The SMS text may be empty (up to 160 chars), so it does not use `validate_body`.
SMS_LOG_BODY_MAX_CODEPOINTS = 160


class SmsLogEnvelope(BaseModel):
    """An `/up` `kind:"sms_log"` payload: the pager's direct SMS, audited and never routed."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    id: str
    ts: int
    peer: str
    dir: Literal["out", "in"]
    st: Literal["sent", "failed", "recv", "blocked"]
    body: str = ""
    sms_ts: int
    # §14.2: present on every signed envelope; optional so password-mode devices are not rejected
    # by shape.
    n: int | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not ID_RE.match(value):
            raise ValueError("invalid id format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int) -> int:
        validate_ts(value)
        return value

    @field_validator("peer")
    @classmethod
    def _check_peer(cls, value: str) -> str:
        if not _SMS_PEER_RE.match(value):
            raise ValueError("sms_log peer is not a valid E.164 phone number")
        return value

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str) -> str:
        if len(value) > SMS_LOG_BODY_MAX_CODEPOINTS:
            raise ValueError("sms_log body exceeds 160 Unicode code points")
        return value

    @field_validator("sms_ts")
    @classmethod
    def _check_sms_ts(cls, value: int) -> int:
        if value < 0:
            raise ValueError("sms_ts must be >= 0")
        return value

    @field_validator("n")
    @classmethod
    def _check_n(cls, value: int | None) -> int | None:
        return _check_n_range(value)
