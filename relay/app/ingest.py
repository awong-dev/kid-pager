"""Business logic for inbound device traffic posted to `/webhooks/mqtt`: signature verification, ack
state machine, dedup and online-edge republish (docs/PROTOCOL.md §4, §5, §14.4)."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Literal

import phonenumbers
from pydantic import BaseModel, ConfigDict, field_validator

from app import alerts, book, devauth, devcfg, devsetup, location, wire
from app.broker import BrokerClient
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import battery as battery_store
from app.store import contacts as contacts_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import messages as messages_store
from app.store import sms as sms_store
from app.store import users as users_store
from app.wire import (
    LocEnvelope,
    SmsLogEnvelope,
    StatusEnvelope,
    UpEnvelope,
    resolve_ts,
    stage_name,
)

logger = logging.getLogger("relay.ingest")

# PROTOCOL.md §4.2 case 3: the one exception to "never auto-reply on MQTT".
UNKNOWN_RECIPIENT_BODY = "unknown recipient"



def bad_number_body(name: str) -> str:
    return f"{name}: number must be 10 digits or start with +"


def blocked_body(name: str) -> str:
    return f"{name}: number not allowed"


def in_book_body(name: str) -> str:
    return f"{name}: already in your book"


def no_contact_body(name: str, alias: str) -> str:
    return f"{name}: no contact @{alias}"


def record_bad_sig(device_id: str) -> None:
    """Count a bad signature as `sigFailures` and raise `devices/{d}.status.authAlarm` after more than
    20 in 10 minutes (docs/PROTOCOL.md §14.4)."""
    device_secrets_store.bump_sig_failures(device_id)
    window_count = devices_store.record_sig_failure(device_id)
    if window_count >= devices_store.AUTH_ALARM_THRESHOLD:
        logger.error(
            "SECURITY authAlarm device=%s: %d bad signatures in the last %ds",
            device_id,
            window_count,
            devices_store.AUTH_ALARM_WINDOW_S,
        )
        devices_store.set_auth_alarm(device_id, True)


# docs/PROTOCOL.md §3.2/§3.1: `name` is 1-16 code points, <=48 UTF-8 bytes.
_CONTACT_NAME_MAX_CODEPOINTS = 16
_CONTACT_NAME_MAX_UTF8_BYTES = 48
_CONTACT_PH_MAX_CHARS = 16
# E.164 shape: `+` then 7-15 digits, first digit 1-9.
_PHONE_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class ContactReqEnvelope(BaseModel):
    """A payload received on `pager/{device_id}/up` with `kind:"contact_req"` (docs/PROTOCOL.md §3.2).

    `ph` is a phone number or an alias reference (docs/PROTOCOL.md §3.1): the envelope checks only
    length and control characters; `classify_ph` decides phone / alias / bad.
    """

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    id: str
    ts: int
    name: str
    ph: str | None = None
    n: int | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not wire.ID_RE.match(value):
            raise ValueError("invalid id format")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: int) -> int:
        wire.validate_ts(value)
        return value

    @field_validator("n")
    @classmethod
    def _check_n(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value < wire.N_MAX_EXCLUSIVE):
            raise ValueError("n out of range")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value:
            raise ValueError("contact_req name must not be empty")
        if wire.CONTROL_CHAR_RE.search(value):
            raise ValueError("contact_req name contains control characters")
        if len(value) > _CONTACT_NAME_MAX_CODEPOINTS:
            raise ValueError("contact_req name exceeds 16 code points")
        if len(value.encode("utf-8")) > _CONTACT_NAME_MAX_UTF8_BYTES:
            raise ValueError("contact_req name exceeds 48 UTF-8 bytes")
        return value

    @field_validator("ph")
    @classmethod
    def _check_ph(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not (1 <= len(value) <= _CONTACT_PH_MAX_CHARS):
            raise ValueError("contact_req ph must be 1-16 characters")
        if wire.CONTROL_CHAR_RE.search(value):
            raise ValueError("contact_req ph contains control characters")
        return value


def classify_ph(ph: str) -> tuple[Literal["phone", "alias", "bad"], str]:
    """Digits are never an alias: 10 digits are `+1` and the digits, 11 starting with `1` are `+` and
    the digits, `+digits` must be a possible E.164 number, any other digit string is bad. Returns
    `(kind, normalized)`: the E.164 number, the alias, or `ph` itself for `bad`."""
    if re.fullmatch(r"[0-9]{10}", ph):
        return "phone", "+1" + ph
    if re.fullmatch(r"1[0-9]{10}", ph):
        return "phone", "+" + ph
    if re.fullmatch(r"\+[0-9]+", ph):
        if not _PHONE_E164_RE.match(ph):
            return "bad", ph
        try:
            ok = phonenumbers.is_possible_number(phonenumbers.parse(ph, None))
        except phonenumbers.NumberParseException:
            ok = False
        return ("phone", ph) if ok else ("bad", ph)
    if ph.isascii() and ph.isdigit():
        return "bad", ph
    if wire.ALIAS_RE.match(ph):
        return "alias", ph
    return "bad", ph


def _has_open_sms_unknown(family_id: str, e164: str) -> bool:
    return any(
        a.kind == "sms_unknown" and a.peerPhone == e164
        for a in alerts_store.list_alerts(family_id, "open")
    )


def _device_id_from_topic(topic: str, expected_suffix: str) -> str | None:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "pager" or parts[2] != expected_suffix:
        return None
    return parts[1]


def _bid_from_boot_up_topic(topic: str) -> str | None:
    """`pager/boot/{bid}/up` (docs/PROTOCOL.md §2): one segment longer than `_device_id_from_topic` parses."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "pager" or parts[1] != "boot" or parts[3] != "up":
        return None
    return parts[2]


        return True
    return env.ota_st in ("ok", "fail", "rb") and env.ota_t in (None, target16)


_E164_RAW = re.compile(rb"\+[1-9]\d{6,14}")
_MSG_ID_RAW = re.compile(rb"s_[0-9a-f]{8}")
# JSON `"dir":"out"` or CBOR key 45 (0x18 0x2d) + text(3|2).
_DIR_RAW = re.compile(rb'(?:"dir"\s*:\s*"|\x18\x2d[\x62\x63])(out|in)')
# JSON `"st":"sent"` or CBOR key 46 (0x18 0x2e) + text header.
_ST_RAW = re.compile(rb'(?:"st"\s*:\s*"|\x18\x2e[\x64-\x67])(sent|failed|recv|blocked)')


def _extract_sms_fields(raw: bytes) -> dict[str, str | None]:
    """Best-effort pulls from an unparseable sms_log envelope's raw bytes."""

    def first(rx: re.Pattern[bytes]) -> str | None:
        m = rx.search(raw)
        if m is None:
            return None
        return (m.group(1) if rx.groups else m.group(0)).decode("ascii")

    return {
        "peer": first(_E164_RAW),
        "dir": first(_DIR_RAW),
        "st": first(_ST_RAW),
        "id": first(_MSG_ID_RAW),
    }


class Ingest:
    """Owns the relay's reaction to device traffic; the webhook router calls it once per event.

    `_secret_cache` holds only the static `deviceSecrets/{d}` key material; the replay-window
    counters are read fresh from the store on every call."""

    def __init__(self, broker: BrokerClient, routing: Routing | None = None) -> None:
        self._broker = broker
        self._routing = routing if routing is not None else Routing(broker)
        self._secret_cache: dict[str, device_secrets_store.DeviceSecret] = {}

    def invalidate_secret_cache(self, device_id: str) -> None:
        """Drop the cached secret so a rotated `hmacKey` is picked up on the next envelope."""
        self._secret_cache.pop(device_id, None)

    def _get_secret(self, device_id: str) -> device_secrets_store.DeviceSecret | None:
        cached = self._secret_cache.get(device_id)
        if cached is not None:
            return cached
        secret = device_secrets_store.get(device_id)
        if secret is not None:
            self._secret_cache[device_id] = secret
        return secret


    def _verify_and_decode(
        self,
        device: devices_store.Device | None,
        device_id: str,
        topic: str,
        payload: bytes,
        *,
        lwt_exception: bool = False,
    ) -> tuple[dict[str, Any], wire.EnvelopeEncoding] | None:
        """Verify before parse (docs/PROTOCOL.md §14.4), for `/up`, `/status` and `/loc`: size check,
        HMAC verify when `authMode == "hmac"` (bad signatures are logged, counted and dropped), decode
        (CBOR or JSON), replay-window check on `n`.

        Returns `None` if the caller should drop the envelope (already logged); otherwise the decoded
        envelope and its wire encoding. `lwt_exception=True` (`handle_status` only) accepts an
        unsigned `{v, state:"offline", session}`, the broker-generated LWT (§14.6).
        """
        if wire.is_oversize(payload):
            wire.log_malformed(topic, payload, "oversize")
            return None

        if device is None or device.authMode != "hmac":
            decoded = wire.decode_envelope_bytes(payload)
            if decoded is None:
                wire.log_malformed(topic, payload, "non-utf8/non-JSON/non-CBOR-object")
                return None
            data, encoding = decoded
            if device is not None:
                devices_store.set_wire(device_id, encoding)
            return data, encoding

        secret = self._get_secret(device_id)
        ok, unsigned = (
            devauth.verify(secret.hmacKey, topic, payload) if secret is not None else (False, b"")
        )
        if not ok:
            if lwt_exception:
                decoded = wire.decode_envelope_bytes(payload)
                if decoded is not None and wire.is_unsigned_lwt_shape(decoded[0]):
                    # docs/PROTOCOL.md §14.6: accepted despite the missing signature; the
                    # `devices/{d}.wire` field and the replay window are not touched (no `n`).
                    return decoded
            logger.warning(
                "SECURITY bad-sig device=%s topic=%s first64=%r", device_id, topic, payload[:64]
            )
            if secret is not None:
                record_bad_sig(device_id)
            return None

        decoded = wire.decode_envelope_bytes(unsigned)
        if decoded is None:
            wire.log_malformed(topic, payload, "signed payload did not decode")
            # Audit trail: an undecodable-but-signed sms_log still leaves a flagged row.
            if b"sms_log" in unsigned and device is not None and device.revokedAt is None:
                self._store_malformed_sms(device_id, unsigned, "signed payload did not decode")
            return None
        data, encoding = decoded
        n = data.get("n")
        if not isinstance(n, int):
            wire.log_malformed(topic, payload, "hmac envelope missing/invalid n")
            return None
        if not device_secrets_store.accept_up_n(device_id, n):
            logger.warning("SECURITY replay device=%s topic=%s n=%s", device_id, topic, n)
            return None
        devices_store.set_wire(device_id, encoding)
        return data, encoding


    def handle_up(self, topic: str, payload: bytes) -> None:
        device_id = _device_id_from_topic(topic, "up")
        if device_id is None:
            logger.warning("up webhook for unrecognised topic %s dropped", topic)
            return
        device = devices_store.get_device(device_id)
        result = self._verify_and_decode(device, device_id, topic, payload)
        if result is None:
            return
        data, _encoding = result

        # `kind:"contact_req"` is dispatched on the raw dict before `UpEnvelope.model_validate`,
        # which rejects any non-null `kind` (docs/PROTOCOL.md §3.4).
        if data.get("kind") == "contact_req":
            self._handle_contact_req(device_id, device, data, topic, payload)
            return

        # `kind:"sms_log"` is dispatched the same way, before `UpEnvelope.model_validate`.
        if data.get("kind") == "sms_log":
            self._handle_sms_log(device_id, device, data, topic, payload)
            return

        try:
            env = UpEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            wire.log_malformed(topic, payload, str(exc))
            return

        if env.is_ack:
            self._handle_ack(device_id, env)
        else:
            self._handle_up_message(device_id, device, env)

    def _handle_contact_req(
        self,
        device_id: str,
        device: devices_store.Device | None,
        data: dict[str, Any],
        topic: str,
        payload: bytes,
    ) -> None:
        path never reaches `_handle_up_message`."""
        if device is None:
            logger.warning(
                "contact_req %s from unregistered device %s dropped",
                data.get("id"),
                device_id,
            )
            return
        if device.revokedAt is not None:
            logger.warning(
                "SECURITY contact_req %s from revoked device %s dropped",
                data.get("id"),
                device_id,
            )
            return

        try:
            env = ContactReqEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            wire.log_malformed(topic, payload, str(exc))
            return

        if contacts_store.get_by_device_and_req(device_id, env.id) is not None:
            logger.info("contact_req %s from device %s is a redelivery", env.id, device_id)
            return
        owner = users_store.get_user(device.ownerUid)
        if owner is None:
            logger.warning("contact_req %s: device %s has no owner user", env.id, device_id)
            return

        outcome, phone, alias = self._classify_contact_req(owner, env)
        logger.info("contact_req outcome=%s device=%s req=%s", outcome, device_id, env.id)

        if outcome == "in_book":
            self._send_system_reply(device_id, in_book_body(env.name), cause_id=env.id)
            return
        if outcome in ("bad_number", "blocked", "no_contact"):
            if outcome == "bad_number":
                body = bad_number_body(env.name)
            elif outcome == "blocked":
                body = blocked_body(env.name)
            else:
                body = no_contact_body(env.name, alias or "")
            contacts_store.create_rejected(
                device_id=device_id,
                owner_uid=device.ownerUid,
                req_id=env.id,
                name=env.name,
                phone=phone,
                alias=alias,
                reason=outcome,
            )
            self._send_system_reply(device_id, body, cause_id=env.id)
            return

                device_id=device_id,
                req_id=env.id,
                name=env.name,
                phone=phone,
                alias=alias,
            )
            return


    def _classify_contact_req(
        self, owner: users_store.User, env: ContactReqEnvelope
    ) -> tuple[str, str | None, str | None]:
        if env.ph is None:
            return "bad_number", None, None
        kind, value = classify_ph(env.ph)
        if kind == "bad":
            return "bad_number", None, None

        if kind == "phone":
            family = (
                families_store.get_family(owner.familyId) if owner.familyId is not None else None
            )
            if family is not None and value in family.blockedNumbers:
                return "blocked", value, None
            # A phone is only ever an SMS contact (a sign-in phone is never a lookup key).

        target = users_store.get_user_by_alias(value)
        if target is not None:
            if target.uid == owner.uid:
                return "in_book", None, value
            inbound = allow_store.get_edge(target.uid, owner.uid)
            if (
                target.kind == "person"
                and not target.disabled
            ):
        return "no_contact", None, value

    def _store_malformed_sms(self, device_id: str, raw: bytes, reason: str) -> None:
        """Best-effort flagged `smsLog` row for an sms_log upload we could not store normally."""
        try:
            fields = _extract_sms_fields(raw)
            log_id = "bad_" + hashlib.sha256(raw).hexdigest()[:12]
            created = sms_store.create_malformed(
                device_id,
                log_id,
                ts=int(time.time()),
                reason=reason[:200],
                raw_hex=raw[:512].hex(),
                peer=fields["peer"],
                dir_=fields["dir"],
                st=fields["st"],
                msg_id=fields["id"],
            )
            logger.info(
                "sms_log malformed device=%s log=%s reason=%s%s",
                device_id,
                log_id,
                reason,
                "" if created else " (duplicate, already stored)",
            )
        except Exception:  # audit is best-effort
            logger.exception("could not store malformed sms_log for device=%s", device_id)

    def _handle_sms_log(
        self,
        device_id: str,
        device: devices_store.Device | None,
        data: dict[str, Any],
        topic: str,
        payload: bytes,
    ) -> None:
        """Store one `devices/{deviceId}/smsLog/{logId}` row (`app/store/sms.py`); not routed to
        anyone. Same drop rules as `_handle_contact_req` for an unregistered/revoked device."""
        if device is None:
            logger.warning(
                "sms_log %s from unregistered device %s dropped", data.get("id"), device_id
            )
            return
        if device.revokedAt is not None:
            logger.warning(
                "SECURITY sms_log %s from revoked device %s dropped", data.get("id"), device_id
            )
            return

        try:
            env = SmsLogEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            wire.log_malformed(topic, payload, str(exc))
            raw = payload
            if device.authMode == "hmac":
                secret = self._get_secret(device_id)
                if secret is not None:
                    ok, unsigned = devauth.verify(secret.hmacKey, topic, payload)
                    if ok:
                        raw = unsigned
            reason = "invalid fields: " + ", ".join(
                sorted({str(e["loc"][0]) for e in getattr(exc, "errors", list)() if e["loc"]})
            )
            self._store_malformed_sms(device_id, raw, reason.rstrip(": "))
            return

        # The device's family attributes the peer: its own contact for that number, or none.
        owner = users_store.get_user(device.ownerUid)
        e164: str | None = None
        peer_uid: str | None = None
        if owner is not None and owner.familyId is not None:
            try:
                e164 = externals_store.normalize_phone(env.peer)
            except ValueError:
                e164 = None
            if e164 is not None:
                contact = externals_store.get_family_contact(owner.familyId, e164)
                peer_uid = contact.uid if contact is not None else None

        created = sms_store.create_log(
            device_id,
            env.id,
            ts=resolve_ts(env.ts),
            sms_ts=env.sms_ts,
            dir_=env.dir,
            peer=env.peer,
            st=env.st,
            body=env.body,
            peer_uid=peer_uid,
        )
        # Logged whether this call created the row or found it already there (broker redelivery);
        # `sms_store.create_log` dedups silently.
        logger.info(
            "sms_log %s device=%s dir=%s peer=%s st=%s%s",
            env.id,
            device_id,
            env.dir,
            env.st,
            "" if created else " (duplicate, already stored)",
        )
        if env.st == "blocked":
        if (
            created
            and env.dir == "in"
            and owner is not None
            and owner.familyId is not None
            and e164 is not None
            and peer_uid is None
            and not _has_open_sms_unknown(owner.familyId, e164)
        ):
            alerts.sms_unknown(owner.familyId, e164, owner.uid, env.body)

    def _handle_ack(self, device_id: str, env: UpEnvelope) -> None:
        assert env.ack is not None and env.ack in ("shown", "read")
        ack_ts = resolve_ts(env.ts)

        # A down message's wire `id` is its `messages/{id}` doc id, except a `book`/`cfg` id, which
        # addresses a device and is checked here after the ordinary lookup misses.
        msg = messages_store.get_message(env.id)
        if msg is None:
            if env.ack == "shown" and devcfg.ack(device_id, env.id):
                return
            # docs/PROTOCOL.md §4.1 rule 3: unknown id -> log + drop, never create a row.
            logger.info("ack for unknown message id=%s from device=%s dropped", env.id, device_id)
            return
        self._handle_v2_ack(device_id, msg, env.ack, ack_ts)

    def _handle_v2_ack(
        self,
        device_id: str,
        msg: messages_store.Message,
        ack: Literal["shown", "read"],
        ack_ts: int,
    ) -> None:
        bid = messages_store.find_pager_delivery(msg, device_id)
        if bid is None:
            # docs/PROTOCOL.md §4.1 rule 4: the message has no pager delivery for this device.
            logger.warning(
                "SECURITY wrong-device ack: pager/%s/up acked message %s with no matching "
                "pager delivery",
                device_id,
                msg.id,
            )
            return
        result = messages_store.apply_delivery_ack(msg.id, bid, device_id, ack, ack_ts)
        if result == "noop":
            logger.debug("idempotent ack '%s' for message %s backend %s", ack, msg.id, bid)

    def _handle_up_message(
        self, device_id: str, device: devices_store.Device | None, env: UpEnvelope
    ) -> None:
        if device is None:
            # Not a registered device: nowhere to route it, dropped like an unknown recipient.
            logger.warning("up message %s from unregistered device %s dropped", env.id, device_id)
            return
        if device.revokedAt is not None:
            # A revoked device is not allowed to inject anything.
            logger.warning(
                "SECURITY up message %s from revoked device %s dropped", env.id, device_id
            )
            return
        self._handle_v2_up_message(device, env)

    def _handle_v2_up_message(self, device: devices_store.Device, env: UpEnvelope) -> None:
        # `routing.send()` does the allow-list check, the transactional dedup by wireId (covers
        # QoS-1 redelivery) and the fan-out to the recipient's enabled backends.
        result = self._routing.send(
            sender_uid=device.ownerUid,
            recipient_alias=env.to,
            kind="text",
            body=env.body,
            origin_backend_kind="pager",
            wire_id=env.id,
            device_id=device.id,
            ts=resolve_ts(env.ts),
        )
            # docs/PROTOCOL.md §4.2 case 3: an unknown or disallowed explicit `to` gets exactly one
            # `system` down reply, never stored as a message, one per offending up message.

    def _send_system_reply(self, device_id: str, body: str, *, cause_id: str) -> None:
        # The reply's `id` is derived from the offending up message's id (docs/PROTOCOL.md §4.2);
        # a fresh id would re-alert on a QoS 1 redelivery.


    def handle_status(self, topic: str, payload: bytes) -> None:
        device_id = _device_id_from_topic(topic, "status")
        if device_id is None:
            logger.warning("status webhook for unrecognised topic %s dropped", topic)
            return
        device = devices_store.get_device(device_id)
        if device is None:
            # Not a registered device: nowhere to store status.
            logger.info("status for unregistered device %s dropped", device_id)
            return
        result = self._verify_and_decode(device, device_id, topic, payload, lwt_exception=True)
        if result is None:
            return
        data, _encoding = result
        try:
            env = StatusEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            wire.log_malformed(topic, payload, str(exc))
            return

        # One INFO line per accepted status for crash diagnostics: reset reason, stage name (raw
        # int if unknown) and abnormal-reset count; `None` on older firmware.
        logger.info(
            "status %s: state=%s link=%s rst=%s stage=%s abn=%s stallcmd=%s loc_move_s=%s",
            device_id,
            env.state,
            env.link,
            env.rst,
            stage_name(env.stage),
            env.abn,
            env.stallcmd if env.stallcmd is not None else "-",
            env.loc_move_s,
        )

        previous_status = device.status
        resolved_ts = resolve_ts(env.ts) if env.ts is not None else None
        devices_store.update_status(
            device_id,
            state=env.state,
            mode=env.mode,
            battMv=env.batt_mv,
            rssi=env.rssi,
            session=env.session,
            ts=resolved_ts,
            fw=env.fw,
            locPeriodS=env.loc_period_s,
            locMinS=env.loc_min_s,
            tls=env.tls,
            caFp=env.ca_fp,
            locBackoffS=env.loc_backoff_s,
            locMoveS=env.loc_move_s,
            smsLost=env.sms_lost,
            link=env.link,
            rst=env.rst,
            stage=env.stage,
            abn=env.abn,
            bpull=env.bpull,
            stallcmd=env.stallcmd,
            img=env.img,
            otaCap=env.ota,
            otaTarget=env.ota_t,
            otaState=env.ota_st,
            otaPct=env.ota_pct,
            otaErr=env.ota_err,
        )

        # One time-series sample per online /status; a malformed `bs` is dropped alone.
        if env.state == "online":
            try:
                bs = battery_store.parse_bs(env.bs, device_id) if env.bs is not None else None
                battery_store.add_sample(device_id, env, resolved_ts, bs)
            except Exception:
                logger.exception("battery sample failed device=%s", device_id)

        # The web's `otaJob` lives until the device reports the job finished (`ok`/`fail`/`rb` for
        # that target) or the new image is running.
            devcfg.clear_ota_job(device_id)

        # On a transition into `broken`, log a security event.
        if env.tls == "broken" and previous_status.tls != "broken":
            logger.error("SECURITY tls-broken device=%s", device_id)

        if env.state != "online":
            # docs/PROTOCOL.md §5.3: bare offline (retained or LWT) -> mark offline only.
            return

        # The first signed `/status` marks the device `provisioned`; only `hmac` devices sign.
        if device.authMode == "hmac" and device.provisionState != "provisioned":
            devices_store.set_provision_state(device_id, "provisioned")

        # Republish before the bv-triggered push so a fresh book is published once. A changed `link`
        # (silent modem session resume, <= 10 s subscription gap) also triggers it.
        link_changed = (
            previous_status.link is not None
            and env.link is not None
            and previous_status.link != env.link
        )
        session_changed = previous_status.session != env.session or link_changed
        offline_to_online = previous_status.state == "offline"
        if session_changed or offline_to_online:
            self._republish_unacked(device_id)

        # docs/PROTOCOL.md §5.3/§3.7: bump a zero `bookVersion` to 1 and publish, else re-nudge if
        # the reported `bv` is behind. `bv` comes from the raw dict (`StatusEnvelope` lacks it).
        reported_bv = data.get("bv")
        if contacts_store.bootstrap_book_version(device_id):
            logger.info("status bv=%s bootstraps devices/%s.bookVersion to 1", reported_bv, device_id)
            devcfg.push_book(device_id, self._broker)
        elif isinstance(reported_bv, int) and reported_bv < devcfg.get_book_version(device_id):
            logger.info(
                "status bv=%s behind devices/%s.bookVersion -- re-nudging/re-pushing book",
                reported_bv,
                device_id,
            )
            devcfg.renudge_if_behind(device_id, reported_bv, self._broker)

    def _republish_unacked(self, device_id: str) -> None:
        """The online-edge re-publish (docs/PROTOCOL.md §5.3): pending messages from `pendingDeviceIds`
        (oldest first, capped at 10, excluding `kind:"loc_req"`), plus the newest unacked `book`/`cfg`,
        one each, which do not count against the cap."""
        for msg in messages_store.list_pending_for_device(device_id):
            if msg.kind == "loc_req":
                continue
            logger.info("re-publishing unacked message %s to device %s", msg.id, device_id)
            self._routing.redeliver_pager(msg, device_id)
        devcfg.republish_pending(device_id, self._broker)


    def handle_loc(self, topic: str, payload: bytes) -> None:
        """Validate per docs/PROTOCOL.md §13.2, then hand off to `app.location.ingest_loc`, which never
        originates a delivery: it only resolves an existing `loc_req` delivery to `fulfilled`."""
        device_id = _device_id_from_topic(topic, "loc")
        if device_id is None:
            logger.warning("loc webhook for unrecognised topic %s dropped", topic)
            return
        device = devices_store.get_device(device_id)
        result = self._verify_and_decode(device, device_id, topic, payload)
        if result is None:
            return
        data, _encoding = result
        try:
            env = LocEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            wire.log_malformed(topic, payload, str(exc))
            return
        location.ingest_loc(device_id, env)


    def handle_boot_ack(self, topic: str, payload: bytes) -> None:
        """The device's bootstrap ack, `{"v":1,"ok":1}` in CBOR (`app/wirecbor.KEYMAP` `v`=0, `ok`=29).
        The bootstrap credential (`boot-{bid}`, ACL'd to publish only `pager/boot/{bid}/up`) is the
        authentication, so there is no signature step. Anything but exactly `{v:1, ok:1}` is dropped
        and logged (docs/PROTOCOL.md §3.4); `devsetup.complete` runs only on a recognised ack."""
        bid = _bid_from_boot_up_topic(topic)
        if bid is None:
            logger.warning("boot-ack webhook for unrecognised topic %s dropped", topic)
            return
        decoded = wire.decode_envelope_bytes(payload)
        if decoded is None:
            wire.log_malformed(topic, payload, "boot ack non-utf8/non-JSON/non-CBOR-object")
            return
        data, _encoding = decoded
        if data.get("v") != 1 or data.get("ok") != 1:
            wire.log_malformed(topic, payload, "boot ack is not {v:1, ok:1}")
            return
        logger.info("boot ack received for bid=%s -- completing bootstrap", bid)
        devsetup.complete(bid, broker=self._broker)
