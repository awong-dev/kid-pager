"""Wire-format validation for the MQTT envelope (docs/PROTOCOL.md §3): validate, never strip.
Applied to device payloads on `/up` and `/status`; the HTTP API strips before a message exists."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal


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

    @field_validator("to")
    @classmethod
    def _check_to(cls, value: str | None) -> str | None:
        # §3.1: same alias shape as `from`; whether `to:"system"` is routable is a store decision.
        if value is not None and not is_valid_alias(value):
            raise ValueError("invalid 'to' alias format")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> UpEnvelope:
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

    @classmethod
    def _check_loc_timing(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value <= 86400):
        return value

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
) -> bytes:
    if kind != "loc_req" and not body:
        raise ValueError("msg down payload requires a non-empty body")
    obj: dict[str, Any] = {"v": v, "id": msg_id, "ts": ts}
    if kind != "msg":
        obj["kind"] = kind
    obj["from"] = from_
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
