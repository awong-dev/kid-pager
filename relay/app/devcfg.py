"""`book` and `cfg` `/down` envelopes (docs/PROTOCOL.md §3.1/§3.2).
Pending state is kept in `devices/{d}.pending*` sub-fields; only the newest of each kind."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import NotFound
from pydantic import BaseModel, ConfigDict, field_validator

from app import ca_resolve
from app.broker import BrokerClient
from app.config import Settings
from app.db.firestore import get_db
from app.ids import new_message_id
from app.movekeys import is_valid_move_id
from app.store import backends as backends_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import users as users_store

# cfg/book/nudge builds stay under the legacy 640-byte envelope so a device that has not
# reported `mb` (older firmware) can still receive them.
from app.wire import LEGACY_MAX_ENVELOPE_BYTES as MAX_ENVELOPE_BYTES
from app.wirecbor import encode as cbor_encode
from app.wirecbor import to_json_safe

logger = logging.getLogger("relay.devcfg")

# The cap for the legacy full `/down book` envelope (`build_book`, non-bpull devices,
# docs/PROTOCOL.md §3.7's gate).
MAX_APPROVED_CONTACTS = 10
# docs/PROTOCOL.md §3.7: the §14.7 fetch response's own `c[]` cap (the device's stored
# capacity).
MAX_PULL_CONTACTS = 32

# docs/PROTOCOL.md §3.1: c[].n / p[].n are display names with a 16-code-point cap, truncated
# here rather than rejected since a stored `displayName` may already exceed it.
_BOOK_NAME_MAX_CODEPOINTS = 16

# docs/PROTOCOL.md §14.3: a CBOR `sig` pair is always exactly 10 bytes on the wire
# (`0x0D 0x48` + an 8-byte tag).
_SIGNED_CBOR_SIG_BYTES = 10
# Worst-case `n` (a 52-bit counter, `app/wire.py`'s `N_MAX_EXCLUSIVE`) for the size check,
# since the real value is only known at publish time.
_WORST_CASE_N = 2**53 - 1

_PENDING_BOOK_FIELD = "pendingBook"
_PENDING_CFG_FIELD = "pendingCfg"
# `cfg.ca` has its own pending slot so a pending lock and a pending CA push do not clobber
# each other. `lock` keeps the original `pendingCfg` name.
_PENDING_CFG_CA_FIELD = "pendingCfgCa"
# `cfg.sms` has its own pending slot, for the same reason.
_PENDING_CFG_SMS_FIELD = "pendingCfgSms"
# `cfg.loc` has its own pending slot.
_PENDING_CFG_LOC_FIELD = "pendingCfgLoc"
# `cfg.ota` (job or cancel) has its own slot; a newer push (including a cancel) replaces the
# older one.
_PENDING_CFG_OTA_FIELD = "pendingCfgOta"
# `cfg.ast` (font job or cancel) has its own slot. Dropped by ingest when an online `/status`
# lacks `ast`, so firmware without font OTA is not re-sent the job.
_PENDING_CFG_AST_FIELD = "pendingCfgAst"
# `cfg.mv` (move order, cancel or proof) has its own slot; the newest replaces the older.
_PENDING_CFG_MV_FIELD = "pendingCfgMv"
# `cfg.br` (bearer-change try or proof) has its own slot; the newest replaces the older.
_PENDING_CFG_BR_FIELD = "pendingCfgBr"
# Every field `ack()`/`republish_pending()` iterate over: nine independent "newest unacked"
# slots.
_ALL_PENDING_FIELDS = (
    _PENDING_BOOK_FIELD,
    _PENDING_CFG_FIELD,
    _PENDING_CFG_CA_FIELD,
    _PENDING_CFG_SMS_FIELD,
    _PENDING_CFG_LOC_FIELD,
    _PENDING_CFG_OTA_FIELD,
    _PENDING_CFG_AST_FIELD,
    _PENDING_CFG_MV_FIELD,
    _PENDING_CFG_BR_FIELD,
)

# docs/MOVE_DESIGN.md §12.1 limits on `cfg.mv`.
MV_HOST_MAX_BYTES = 127
MV_CA_URL_MAX_BYTES = 200
_MV_HOST_RE = re.compile(r"[A-Za-z0-9._:\[\]-]+")


def _devices():
    return get_db().collection("devices")


def get_book_version(device_id: str) -> int:
    """Raw read of `devices/{d}.bookVersion` (not modelled on `devices_store.Device`)."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return 0
    return int((snap.to_dict() or {}).get("bookVersion", 0) or 0)


def _default_alias(device: devices_store.Device) -> str | None:
    if device.defaultToUid is None:
        return None
    user = users_store.get_user(device.defaultToUid)
    return user.alias if user is not None else None


def _contact_type_hint(uid: str) -> str:
    """`c[].t` icon hint (docs/PROTOCOL.md §3.1): `chat` for a `gchat` backend, else `web`."""
    kinds = {b.kind for b in backends_store.list_backends(uid) if b.enabled}
    if "gchat" in kinds:
        return "chat"
    return "web"


def _fed_type(device: devices_store.Device) -> str:
    """`fed` once the pager has reported `mb` (it understands receipts), else `web`."""
    return "fed" if device.status.mb is not None else "web"


def _group_contacts(owner_uid: str) -> list[dict[str, Any]]:
    """Every group `owner_uid` belongs to as a `t: "grp"` contact; alias-less groups are skipped."""
    return [
        {"a": conv.alias, "n": (conv.name or conv.alias)[:_BOOK_NAME_MAX_CODEPOINTS], "t": "grp"}
        for conv in conversations_store.list_groups_for_member(owner_uid)
        if conv.alias is not None
    ]


def _approved_contacts(owner_uid: str, *, fed_type: str = "web") -> list[dict[str, Any]]:
    """Every contact the device book may list (sendable or `added`), uncapped and unordered."""
    from app import book

    return [
        {
            "a": e.alias,
            "n": e.label[:_BOOK_NAME_MAX_CODEPOINTS],
            "t": (
                "grp"
                if e.kind == "group"
                else fed_type
                if e.fed
                else ("chat" if e.chat else "sms")
                if e.kind == "external"
                else _contact_type_hint(e.uid)  # type: ignore[arg-type]
            ),
        }
        for e in book.entries_for(owner_uid)
        if book.listed_on_pager(e)
    ]


def _contact_sort_key(contact: dict[str, Any], default_alias: str | None) -> tuple[int, str, str]:
    """docs/PROTOCOL.md §3.7 `c[]` order: `d` first, then groups, then the rest by display name."""
    if contact["a"] == default_alias:
        rank = 0
    elif contact["t"] == "grp":
        rank = 1
    else:
        rank = 2
    return (rank, contact["n"].casefold(), contact["a"])


def _ordered_contacts(
    owner_uid: str, default_alias: str | None, *, fed_type: str = "web"
) -> list[dict[str, Any]]:
    """`_approved_contacts` in §3.7 `c[]` order, uncapped."""
    contacts = _approved_contacts(owner_uid, fed_type=fed_type)
    contacts.sort(key=lambda c: _contact_sort_key(c, default_alias))
    return contacts


# `sig` is 11 base64url characters at runtime (8 raw bytes, `app/devauth.py`'s 64-bit
# truncated tag); `,"sig":"<11 chars>"` is the tail `sign_json` appends.
_SIGNED_JSON_SIG_SUFFIX_BYTES = len(',"sig":""') + 11


def _assert_within_envelope_limit(obj: dict[str, Any]) -> None:
    """Assert the signed envelope fits `MAX_ENVELOPE_BYTES` in both encodings (worst-case `n`)."""
    worst_case = {**obj, "n": _WORST_CASE_N}
    cbor_len = len(cbor_encode(worst_case)) + _SIGNED_CBOR_SIG_BYTES
    assert cbor_len <= MAX_ENVELOPE_BYTES, (
        f"devcfg {obj.get('kind')!r} payload too large: {cbor_len} bytes "
        f"signed CBOR (limit {MAX_ENVELOPE_BYTES})"
    )
    # `to_json_safe` mirrors `devauth.sign_json`: a raw `bytes` leaf (e.g. `cfg.ca.sha`) is
    # base64url text on the JSON wire.
    json_len = (
        len(
            json.dumps(
                to_json_safe(worst_case), separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        + _SIGNED_JSON_SIG_SUFFIX_BYTES
    )
    assert json_len <= MAX_ENVELOPE_BYTES, (
        f"devcfg {obj.get('kind')!r} payload too large: {json_len} bytes "
        f"signed JSON (limit {MAX_ENVELOPE_BYTES})"
    )


def build_book(device_id: str) -> dict[str, Any]:
    """The legacy full `/down book` envelope, capped at `MAX_APPROVED_CONTACTS`."""
    device = devices_store.get_device(device_id)
    if device is None:
        raise ValueError(f"no such device: {device_id!r}")

    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "book",
        "bv": get_book_version(device_id),
    }
    default_alias = _default_alias(device)
    if default_alias is not None:
        obj["d"] = default_alias
    obj["c"] = _ordered_contacts(
        device.ownerUid, default_alias, fed_type=_fed_type(device)
    )[:MAX_APPROVED_CONTACTS]
    obj["ack"] = None
    _assert_within_envelope_limit(obj)
    return obj


def build_book_body(device_id: str) -> dict[str, Any]:
    """The §14.7 fetch response body (§3.7): unsigned, no `id`/`ack`, `c[]` capped at 32."""
    device = devices_store.get_device(device_id)
    if device is None:
        raise ValueError(f"no such device: {device_id!r}")

    default_alias = _default_alias(device)
    contacts = _ordered_contacts(device.ownerUid, default_alias, fed_type=_fed_type(device))
    truncated = len(contacts) > MAX_PULL_CONTACTS

    obj: dict[str, Any] = {
        "v": 1,
        "ts": int(time.time()),
        "kind": "book",
        "bv": get_book_version(device_id),
    }
    if default_alias is not None:
        obj["d"] = default_alias
    obj["c"] = contacts[:MAX_PULL_CONTACTS]
    if truncated:
        obj["more"] = True
    return obj


def build_nudge(device_id: str, url: str) -> dict[str, Any]:
    """docs/PROTOCOL.md §3.7: the `/down book` nudge, `bv` and `url` only."""
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "book",
        "bv": get_book_version(device_id),
        "url": url,
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    return obj


def _set_pending(device_id: str, field: str, obj: dict[str, Any]) -> None:
    """Replaces the whole slot; `set(merge=True)` would keep stale `c`/`p`/`d` in a map."""
    value = {field: {"id": obj["id"], "obj": obj, "acked": False}}
    ref = _devices().document(device_id)
    try:
        ref.update(value)
    except NotFound:
        ref.set(value, merge=True)


def _clean_pending_obj(obj: dict[str, Any]) -> dict[str, Any]:
    """A stored `book` carrying `url` is a nudge: drop leftover full-book keys."""
    if obj.get("kind") == "book" and "url" in obj:
        return {k: v for k, v in obj.items() if k not in ("c", "p", "d", "more")}
    return obj


def push_book(
    device_id: str, broker: BrokerClient, *, settings: Settings | None = None
) -> bool:
    """Build and publish the pending book; a nudge only if `bpull == 1`, hmac, base URL (§3.7)."""
    device = devices_store.get_device(device_id)
    if device is None:
        logger.warning("push_book: no such device %s", device_id)
        return False

    obj: dict[str, Any] | None = None
    if device.status.bpull == 1 and device.authMode == "hmac":
        settings = settings if settings is not None else Settings.from_env()
        base = settings.public_base_url
        if base:
            obj = build_nudge(device_id, f"{base.rstrip('/')}/api/device/book")
        else:
            logger.error(
                "push_book: device %s advertises bpull=1 but "
                "settings.public_base_url is not configured -- falling "
                "back to the legacy full book (never publishing a nudge "
                "without a url)",
                device_id,
            )
    if obj is None:
        obj = build_book(device_id)

    _set_pending(device_id, _PENDING_BOOK_FIELD, obj)
    return broker.publish_down(device_id, obj)


def renudge_if_behind(
    device_id: str, reported_bv: int, broker: BrokerClient, *, settings: Settings | None = None
) -> bool:
    """docs/PROTOCOL.md §5.3: re-publish the pending nudge if its `bv` is current, else push."""
    current_bv = get_book_version(device_id)
    snap = _devices().document(device_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    pending = data.get(_PENDING_BOOK_FIELD)
    if isinstance(pending, dict) and not pending.get("acked", False):
        obj = pending.get("obj")
        if isinstance(obj, dict) and "url" in obj and obj.get("bv") == current_bv:
            logger.info(
                "re-publishing pending unacked book nudge %s to device %s "
                "(reported bv=%s behind bv=%s, pending nudge already matches)",
                obj.get("id"),
                device_id,
                reported_bv,
                current_bv,
            )
            return broker.publish_down(device_id, _clean_pending_obj(obj))
    return push_book(device_id, broker, settings=settings)


def push_cfg(device_id: str, lock: dict[str, Any], broker: BrokerClient) -> bool:
    """`lock` is the `{clear?: bool, auto?: int}` map `POST /api/admin/devices/{id}/cfg` accepts."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_cfg: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"lock": lock},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_ca(
    device_id: str, *, pem: str, broker: BrokerClient, settings: Settings | None = None
) -> bool:
    """`/down cfg.ca = {url, sha}` for `pem`; raises `PublicBaseUrlRequired` without a base URL."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_ca: no such device %s", device_id)
        return False
    settings = settings if settings is not None else Settings.from_env()
    url, sha = ca_resolve.ca_pointer(pem, settings)
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ca": {"url": url, "sha": sha}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_CA_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_ota(
    device_id: str,
    cfg_ota: dict[str, Any],
    broker: BrokerClient,
    *,
    job: dict[str, Any] | None = None,
) -> bool:
    """`/down cfg.ota`; `job`, if given, is stored as `devices/{d}.otaJob` for the web."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_ota: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ota": cfg_ota},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_OTA_FIELD, obj)
    if job is not None:
        _devices().document(device_id).set({"otaJob": {**job, "at": datetime.now(UTC)}}, merge=True)
    return broker.publish_down(device_id, obj)


def cancel_ota(device_id: str, broker: BrokerClient) -> bool:
    """`cfg.ota = {cancel: true}` and clears `otaJob`."""
    if devices_store.get_device(device_id) is None:
        logger.warning("cancel_ota: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ota": {"cancel": True}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_OTA_FIELD, obj)
    clear_ota_job(device_id)
    return broker.publish_down(device_id, obj)


def push_ast(
    device_id: str,
    cfg_ast: dict[str, Any],
    broker: BrokerClient,
    *,
    job: dict[str, Any] | None = None,
) -> bool:
    """`/down cfg.ast`; `job`, if given, is stored as `devices/{d}.otaJob` with `part: "assets"`."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_ast: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ast": cfg_ast},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_AST_FIELD, obj)
    if job is not None:
        _devices().document(device_id).set(
            {"otaJob": {**job, "part": "assets", "at": datetime.now(UTC)}}, merge=True
        )
    return broker.publish_down(device_id, obj)


def cancel_ast(device_id: str, broker: BrokerClient) -> bool:
    """`cfg.ast = {cancel: true}` and clears `otaJob`."""
    if devices_store.get_device(device_id) is None:
        logger.warning("cancel_ast: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ast": {"cancel": True}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_AST_FIELD, obj)
    clear_ota_job(device_id)
    return broker.publish_down(device_id, obj)


def drop_pending_ast(device_id: str) -> None:
    """Deletes `devices/{d}.pendingCfgAst` (a no-op if absent)."""
    from google.cloud.firestore import DELETE_FIELD

    ref = _devices().document(device_id)
    try:
        ref.update({_PENDING_CFG_AST_FIELD: DELETE_FIELD})
    except NotFound:
        pass


def clear_ota_job(device_id: str) -> None:
    """Sets `devices/{d}.otaJob` to null (job cancelled or reported done)."""
    _devices().document(device_id).set({"otaJob": None}, merge=True)


class MoveCa(BaseModel):
    """The target's CA pointer from its `move-info` (`ca.url`, `ca.sha` as 64 hex)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    sha: bytes

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("https://") or len(v.encode("utf-8")) > MV_CA_URL_MAX_BYTES:
            raise ValueError("cfg.mv ca url must be https and at most 200 bytes")
        return v

    @field_validator("sha", mode="before")
    @classmethod
    def _sha(cls, v: object) -> object:
        if isinstance(v, str):
            if re.fullmatch(r"[0-9a-f]{64}", v) is None:
                raise ValueError("cfg.mv ca sha must be 64 lowercase hex")
            return bytes.fromhex(v)
        return v

    @field_validator("sha")
    @classmethod
    def _sha_len(cls, v: bytes) -> bytes:
        if len(v) != 32:
            raise ValueError("cfg.mv ca sha must be 32 bytes")
        return v


def _mv_envelope(cfg_mv: dict[str, Any]) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"mv": cfg_mv},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    return obj


def _check_move_id(move_id: str) -> None:
    if not is_valid_move_id(move_id):
        raise ValueError(f"invalid move id {move_id!r}")


def build_move_order(
    *, move_id: str, host: str, port: int, ca: MoveCa | None
) -> dict[str, Any]:
    """The `cfg.mv` order `{id, host, port[, ca]}`; `ValueError` for a field outside §12.1."""
    _check_move_id(move_id)
    if not 1 <= len(host.encode("utf-8")) <= MV_HOST_MAX_BYTES or _MV_HOST_RE.fullmatch(host) is None:
        raise ValueError(f"invalid move host {host!r}")
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(f"invalid move port {port!r}")
    mv: dict[str, Any] = {"id": move_id, "host": host, "port": port}
    if ca is not None:
        mv["ca"] = {"url": ca.url, "sha": ca.sha}
    return _mv_envelope(mv)


def build_move_cancel(move_id: str) -> dict[str, Any]:
    """The `cfg.mv` cancel `{id, cancel: true}`."""
    _check_move_id(move_id)
    return _mv_envelope({"id": move_id, "cancel": True})


def build_move_proof(move_id: str, n: int) -> dict[str, Any]:
    """The `cfg.mv` proof `{id, ok: n}`, `n` being the `/status` envelope's `n` (0 to 2^53-1)."""
    _check_move_id(move_id)
    if isinstance(n, bool) or not 0 <= n <= _WORST_CASE_N:
        raise ValueError(f"invalid proof n {n!r}")
    return _mv_envelope({"id": move_id, "ok": n})


def _push_move(device_id: str, obj: dict[str, Any], broker: BrokerClient) -> bool:
    _set_pending(device_id, _PENDING_CFG_MV_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_move_order(
    device_id: str,
    *,
    move_id: str,
    host: str,
    port: int,
    ca: MoveCa | None,
    broker: BrokerClient,
) -> bool:
    """Stores and publishes a `cfg.mv` order under `pendingCfgMv`.

    False, with nothing stored, for an unknown device or one whose last `/status` lacked `mvc: 1`;
    otherwise the publish result. A re-sent order after an unacked one replaces the slot."""
    device = devices_store.get_device(device_id)
    if device is None:
        logger.warning("push_move_order: no such device %s", device_id)
        return False
    if device.status.mvc != 1:
        logger.warning("push_move_order: device %s did not report mvc", device_id)
        return False
    obj = build_move_order(move_id=move_id, host=host, port=port, ca=ca)
    return _push_move(device_id, obj, broker)


def cancel_move(device_id: str, move_id: str, broker: BrokerClient) -> bool:
    """Stores and publishes a `cfg.mv` cancel, replacing an unacked order in the slot."""
    if devices_store.get_device(device_id) is None:
        logger.warning("cancel_move: no such device %s", device_id)
        return False
    return _push_move(device_id, build_move_cancel(move_id), broker)


def push_move_proof(device_id: str, move_id: str, n: int, broker: BrokerClient) -> bool:
    """Stores and publishes a `cfg.mv` proof `{id, ok: n}`; every re-publish is re-signed."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_move_proof: no such device %s", device_id)
        return False
    return _push_move(device_id, build_move_proof(move_id, n), broker)


def _br_envelope(cfg_br: dict[str, Any]) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"br": cfg_br},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    return obj


def build_bearer_try(request_id: str) -> dict[str, Any]:
    """The `cfg.br` try `{id, b: "beam"}` (docs/PROTOCOL.md §3.2)."""
    _check_move_id(request_id)
    return _br_envelope({"id": request_id, "b": "beam"})


def build_bearer_proof(request_id: str) -> dict[str, Any]:
    """The `cfg.br` proof `{id, ok: true}`."""
    _check_move_id(request_id)
    return _br_envelope({"id": request_id, "ok": True})


def push_bearer_try(device_id: str, request_id: str, broker: BrokerClient) -> bool:
    """Stores and publishes a `cfg.br` try under `pendingCfgBr`. False, with nothing stored, for an
    unknown device or one whose last `/status` lacked `brc: 1`."""
    device = devices_store.get_device(device_id)
    if device is None or device.status.brc != 1:
        logger.warning("push_bearer_try: device %s is unknown or did not report brc", device_id)
        return False
    obj = build_bearer_try(request_id)
    _set_pending(device_id, _PENDING_CFG_BR_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_bearer_proof(device_id: str, request_id: str, broker: BrokerClient) -> bool:
    """Stores and publishes a `cfg.br` proof, replacing the try in the slot."""
    if devices_store.get_device(device_id) is None:
        return False
    obj = build_bearer_proof(request_id)
    _set_pending(device_id, _PENDING_CFG_BR_FIELD, obj)
    return broker.publish_down(device_id, obj)


def unpin_ca(device_id: str, broker: BrokerClient) -> bool:
    """`url = ""` means un-pin (docs/PROTOCOL.md §3.2); no `sha`, since there is no CA."""
    if devices_store.get_device(device_id) is None:
        logger.warning("unpin_ca: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ca": {"url": ""}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_CA_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_sms_contacts(
    device_id: str, contacts: list[dict[str, Any]], broker: BrokerClient
) -> bool:
    """`/down cfg.sms = [{n, p}, ...]`, the whole SMS allow-list every time (`[]` clears it)."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_sms_contacts: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"sms": [{"n": c["name"], "p": c["phone"]} for c in contacts]},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_SMS_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_loc(device_id: str, *, gnss: bool, broker: BrokerClient) -> bool:
    """`/down cfg.loc = {gnss}`, under its own pending slot (`_PENDING_CFG_LOC_FIELD`)."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_loc: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"loc": {"gnss": gnss}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_LOC_FIELD, obj)
    return broker.publish_down(device_id, obj)


def loc_pending(device_id: str) -> bool:
    """True iff this device has a pushed `cfg.loc` not yet acked `shown`."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return False
    pending = (snap.to_dict() or {}).get(_PENDING_CFG_LOC_FIELD)
    return isinstance(pending, dict) and not pending.get("acked", False)


def sms_pending(device_id: str) -> bool:
    """True iff this device has a pushed `cfg.sms` not yet acked `shown`."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return False
    pending = (snap.to_dict() or {}).get(_PENDING_CFG_SMS_FIELD)
    return isinstance(pending, dict) and not pending.get("acked", False)


def ack(device_id: str, msg_id: str) -> bool:
    """True if `msg_id` is a pending book/cfg id (idempotent); False for an unknown id."""
    ref = _devices().document(device_id)
    snap = ref.get()
    if not snap.exists:
        return False
    data = snap.to_dict() or {}
    matched = False
    updates: dict[str, Any] = {}
    for field in _ALL_PENDING_FIELDS:
        pending = data.get(field)
        if isinstance(pending, dict) and pending.get("id") == msg_id:
            matched = True
            if not pending.get("acked"):
                updates[f"{field}.acked"] = True
    if updates:
        ref.update(updates)
    return matched


def republish_pending(
    device_id: str,
    broker: BrokerClient,
    *,
    withhold: Callable[[dict[str, Any]], bool] | None = None,
) -> None:
    """docs/PROTOCOL.md §5.3: re-publish the exact previously-built `obj` of each unacked slot,
    except one for which `withhold(obj)` is true."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return
    data = snap.to_dict() or {}
    for field in _ALL_PENDING_FIELDS:
        pending = data.get(field)
        if not isinstance(pending, dict) or pending.get("acked"):
            continue
        obj = pending.get("obj")
        if not isinstance(obj, dict):
            continue
        if withhold is not None and withhold(obj):
            logger.info("withholding unacked %s %s from device %s", obj.get("kind"), obj.get("id"), device_id)
            continue
        logger.info(
            "re-publishing unacked %s %s to device %s", obj.get("kind"), obj.get("id"), device_id
        )
        broker.publish_down(device_id, _clean_pending_obj(obj))
