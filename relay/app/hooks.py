"""Message hooks: signed filter calls before storing, processor delivery after; bodies are never logged.
`X-KP-Signature: sha256=hmac_sha256(key, ts + "." + body)`, `key` being the 32 raw secret bytes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app import alerts as alerts_module
from app.ids import new_id
from app.store import backends as backends_store
from app.store import conversations as conversations_store
from app.store import hooks as hooks_store
from app.store import messages as messages_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.store.hooks import Hook
from app.store.users import User
from app.wire import strip_control_chars

logger = logging.getLogger("relay.hooks")

PAYLOAD_VERSION = 1
CHAIN_BUDGET_S = 15.0
REPLACE_BODY_MAX_BYTES = 8192
ALERT_TITLE_MAX = 60
ALERT_BODY_MAX = 200
REASON_MAX = 200
LABEL_MAX = 24
LABELS_MAX = 8
NOTIFY_MAX = 200
FLAG_DEDUP_WINDOW_S = 3600
HOOK_UNAVAILABLE = "hook_unavailable"

# `message.origin` values.
_ORIGINS = {
    "pager": "pager",
    "webapp": "webapp",
    "sms": "sms",
    "bridge": "bridge",
    "gchat": "gchat",
    "fed": "fed",
}


_client: httpx.Client | None = None


def set_client(client: httpx.Client | None) -> None:
    """Install a test client (e.g. `httpx.MockTransport`); `None` restores the default."""
    global _client
    _client = client


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(follow_redirects=False)
    return _client


def sign(key: bytes, ts: int | str, body: bytes) -> str:
    mac = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def server_name() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "")
    return urlsplit(base).netloc or "localhost"


@dataclass
class MessageCtx:
    """Everything one logical send tells a hook. Built once per send, only when
    some hook covers it."""

    sender: User
    recipients: list[User]
    conv_key: str
    conv_kind: Literal["dm", "group"]
    conv_name: str | None
    origin: str
    ref: str
    ts: int
    family_ids: set[str]
    _history: list[dict] | None = field(default=None, repr=False)

    def history(self, n: int) -> list[dict]:
        """The last `n` text messages of the conversation, oldest first."""
        if n <= 0:
            return []
        if self._history is None or len(self._history) < n:
            self._history = conversation_history(
                self.conv_key, limit=n, group=self.conv_kind == "group"
            )
        return self._history[-n:]


def family_of(user: User) -> str | None:
    return user.familyId if user.kind == "person" else user.ownerFamilyId


def _via(origin: str, sender: User) -> str | None:
    if origin == "sms":
        return "sms"
    if origin == "gchat":
        return "chat"
    if origin == "bridge":
        return "whatsapp" if (sender.chat or {}).get("source") == "whatsapp" else "chat"
    if origin == "fed":
        return "fed"
    return None


def build_ctx(
    *,
    sender_uid: str,
    recipient_uids: list[str],
    conv_key: str,
    conv_kind: Literal["dm", "group"],
    conv_name: str | None,
    origin_backend_kind: str,
    wire_id: str | None,
    ts: int,
) -> MessageCtx | None:
    sender = users_store.get_user(sender_uid)
    recipients = [
        u
        for u in (users_store.get_user(uid) for uid in recipient_uids)
        if u is not None
    ]
    if sender is None or not recipients:
        return None
    fams = {f for f in (family_of(u) for u in [sender, *recipients]) if f}
    return MessageCtx(
        sender=sender,
        recipients=recipients,
        conv_key=conv_key,
        conv_kind=conv_kind,
        conv_name=conv_name,
        origin=_ORIGINS.get(origin_backend_kind, origin_backend_kind),
        ref=wire_id or new_id("hr_"),
        ts=ts,
        family_ids=fams,
    )


HISTORY_SCAN_MAX = 2000


def conversation_history(
    conv_key: str, *, limit: int, group: bool, before_id: str | None = None
) -> list[dict]:
    """The payload's `context` shape: `{id, ts, sender (alias), senderUid, body}`,
    oldest first. A group message is stored once per member; the copies are
    folded on `groupMsgId`. `before_id` (an `id` from an earlier answer) pages
    backwards for the callback API; an id not found in the newest
    `HISTORY_SCAN_MAX` rows yields nothing."""
    members = 1
    if group:
        conv = conversations_store.get_conversation(conv_key)
        members = max(1, len(conv.uids) if conv is not None else 1)
    scan = limit * members if before_id is None else HISTORY_SCAN_MAX
    msgs = messages_store.list_thread(conv_key, limit=scan)
    before_seq: int | None = None
    if before_id is not None:
        before_seq = next((m.seq for m in msgs if (m.groupMsgId or m.id) == before_id), None)
        if before_seq is None:
            return []
    out: list[dict] = []
    seen: set[str] = set()
    aliases: dict[str, str] = {}
    for m in msgs:
        if m.kind != "text" or not m.body:
            continue
        if before_seq is not None and m.seq >= before_seq:
            continue
        key = m.groupMsgId or m.id
        if key in seen:
            continue
        seen.add(key)
        alias = m.senderAlias
        if alias is None:
            if m.senderUid not in aliases:
                u = users_store.get_user(m.senderUid)
                aliases[m.senderUid] = u.alias if u is not None else m.senderUid
            alias = aliases[m.senderUid]
        out.append(
            {
                "id": key,
                "ts": m.ts,
                "sender": alias,
                "senderUid": m.senderUid,
                "body": m.body,
                "_seq": m.seq,
            }
        )
    out = out[-limit:]
    return [{k: v for k, v in e.items() if k != "_seq"} for e in out]


def build_payload(
    ctx: MessageCtx,
    *,
    mode: Literal["filter", "processor"],
    body: str,
    event_id: str,
    context: int,
    message_ids: list[str] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "ref": ctx.ref,
        "kind": "text",
        "body": body,
        "ts": ctx.ts,
        "origin": ctx.origin,
    }
    if message_ids is not None:
        message["ids"] = message_ids
    s = ctx.sender
    return {
        "v": PAYLOAD_VERSION,
        "event": "message",
        "mode": mode,
        "id": event_id,
        "ts": int(time.time()),
        "server": server_name(),
        "message": message,
        "sender": {
            "uid": s.uid,
            "alias": s.alias,
            "name": s.displayName,
            "kind": s.kind,
            "role": s.role,
            "familyId": family_of(s),
            "via": _via(ctx.origin, s),
        },
        "recipients": [
            {
                "uid": r.uid,
                "alias": r.alias,
                "name": r.displayName,
                "kind": r.kind,
                "familyId": family_of(r),
                "backends": sorted(
                    {b.kind for b in backends_store.list_backends(r.uid) if b.enabled}
                ),
            }
            for r in ctx.recipients
        ],
        "conversation": {
            "key": ctx.conv_key,
            "kind": ctx.conv_kind,
            "name": ctx.conv_name,
        },
        "context": ctx.history(context),
    }


def request_for(hook: Hook, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    ts = str(int(time.time()))
    return body, {
        "Content-Type": "application/json",
        "X-KP-Hook": hook.id,
        "X-KP-Timestamp": ts,
        "X-KP-Signature": sign(hook.key(), ts, body),
    }


class _Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    body: str = ""


class FilterResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["allow", "block", "replace"]
    body: str | None = None
    reason: str | None = None
    notifySender: str | None = None
    alert: _Alert | None = None
    labels: list[str] | None = None


class HookError(Exception):
    def __init__(self, msg: str, *, timeout: bool = False) -> None:
        super().__init__(msg)
        self.timeout = timeout


@dataclass
class ChainResult:
    body: str
    blocked: bool = False
    notify: str | None = None
    block_reason: str | None = None
    # hook id -> the `hooks.{hookId}` annotation stored on the message
    annotations: dict[str, dict] = field(default_factory=dict)


def active_hooks() -> list[Hook]:
    """Enabled hooks from the 30 s cache; `[]` is the common, free answer."""
    return hooks_store.enabled_cached()


def clean_labels(labels: list[str] | None) -> list[str]:
    out = [str(x)[:LABEL_MAX] for x in (labels or []) if str(x)]
    return out[:LABELS_MAX]


def _call_filter(
    hook: Hook, ctx: MessageCtx, body: str, *, timeout_s: float
) -> FilterResponse:
    event_id = new_id("he_") + os.urandom(4).hex()
    payload = build_payload(
        ctx, mode="filter", body=body, event_id=event_id, context=hook.context
    )
    raw, headers = request_for(hook, payload)
    started = time.monotonic()
    try:
        resp = get_client().post(
            hook.url, content=raw, headers=headers, timeout=timeout_s
        )
    except httpx.TimeoutException as exc:
        raise HookError("timeout", timeout=True) from exc
    except httpx.HTTPError as exc:
        raise HookError(f"transport {type(exc).__name__}") from exc
    finally:
        logger.info(
            "hook call hook=%s event=%s mode=filter ms=%d",
            hook.id,
            event_id,
            int((time.monotonic() - started) * 1000),
        )
    if resp.status_code != 200:
        raise HookError(f"http {resp.status_code}")
    try:
        parsed = FilterResponse.model_validate(resp.json())
    except (ValueError, ValidationError) as exc:
        raise HookError("bad response") from exc
    return parsed


def run_filters(
    hooks: list[Hook],
    ctx: MessageCtx,
    body: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ChainResult:
    """Runs every covering enabled filter in `order`; each sees the body the
    previous one left, and the first `block` ends the chain. `hooks` is the
    cached enabled list; this picks the filters that cover `ctx`."""
    result = ChainResult(body=body)
    started = clock()
    for hook in hooks:
        if hook.mode != "filter" or not hook.covers(ctx.family_ids):
            continue
        remaining = CHAIN_BUDGET_S - (clock() - started)
        try:
            if remaining <= 0:
                raise HookError("chain budget exhausted")
            resp = _call_filter(
                hook,
                ctx,
                result.body,
                timeout_s=min(hook.timeoutMs / 1000.0, remaining),
            )
            new_body = result.body
            if resp.action == "replace":
                new_body = strip_control_chars(resp.body or "")
                if (
                    not new_body.strip()
                    or len(new_body.encode("utf-8")) > REPLACE_BODY_MAX_BYTES
                ):
                    raise HookError("replace body invalid")
        except HookError as exc:
            _note_error(hook, exc)
            if hook.failMode == "closed":
                result.blocked = True
                result.block_reason = HOOK_UNAVAILABLE
                return result
            continue
        hooks_store.record_call(hook.id, ok=True)
        labels = clean_labels(resp.labels)
        reason = resp.reason[:REASON_MAX] if resp.reason else None
        if labels or reason or resp.action != "allow":
            result.annotations[hook.id] = {
                "action": resp.action,
                "labels": labels,
                "reason": reason,
                "at": int(time.time()),
            }
        if resp.alert is not None and (resp.alert.title or resp.alert.body):
            raise_flag(
                hook,
                family_ids=ctx.family_ids,
                conv_key=ctx.conv_key,
                subject=ctx.sender,
                title=resp.alert.title,
                body=resp.alert.body,
            )
        if resp.action == "block":
            result.blocked = True
            result.block_reason = reason
            if resp.notifySender:
                result.notify = (
                    strip_control_chars(resp.notifySender)[:NOTIFY_MAX] or None
                )
            return result
        result.body = new_body
    return result


def _note_error(hook: Hook, exc: HookError) -> None:
    logger.warning("hook error hook=%s err=%s", hook.id, exc)
    consecutive = hooks_store.record_call(
        hook.id, ok=False, timeout=exc.timeout, error=str(exc)
    )
    if consecutive == hooks_store.CONSECUTIVE_ERRORS_FOR_DOWN:
        raise_down(hook, str(exc))


# Attempt delays: now, 30 s, 5 min; then the delivery is dropped and counted.
RETRY_DELAYS_S: tuple[float, ...] = (0.0, 30.0, 300.0)
PROCESSOR_TIMEOUT_S = 10.0


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="hook-processor", daemon=True).start()


_spawn: Callable[[Callable[[], None]], None] = _default_spawn
_sleep: Callable[[float], None] = time.sleep
_delays: tuple[float, ...] = RETRY_DELAYS_S


def spawn(fn: Callable[[], None]) -> None:
    """Runs `fn` on the background seam (a daemon thread; inline under
    `set_background(spawn=...)` in tests). `app/fed/sender.py` shares it."""
    _spawn(fn)


def set_background(
    *,
    spawn: Callable[[Callable[[], None]], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    delays: tuple[float, ...] | None = None,
) -> None:
    """Test seam: run a delivery inline (`spawn=lambda fn: fn()`), record the
    waits instead of sleeping, shorten the schedule. No arguments restore the
    defaults (a daemon thread per delivery, `time.sleep`, 0 / 30 s / 5 min).

    The queue is in-process: on a host that gets no CPU after the response
    (Cloud Run with request-based billing) the 30 s and 5 min retries may not
    run; the first attempt, started with the response, usually does."""
    global _spawn, _sleep, _delays
    _spawn = spawn or _default_spawn
    _sleep = sleep or time.sleep
    _delays = delays or RETRY_DELAYS_S


def dispatch_processors(
    hooks: list[Hook], ctx: MessageCtx, *, body: str, message_ids: list[str]
) -> int:
    """Hands the stored message to every covering enabled processor, in the
    background, without delaying the sender. Returns how many were queued."""
    queued = 0
    for hook in hooks:
        if hook.mode != "processor" or not hook.covers(ctx.family_ids):
            continue
        _spawn(lambda h=hook: _deliver_processor(h, ctx, body, message_ids))
        queued += 1
    return queued


def _deliver_processor(hook: Hook, ctx: MessageCtx, body: str, message_ids: list[str]) -> None:
    event_id = new_id("he_") + os.urandom(4).hex()
    try:
        payload = build_payload(
            ctx,
            mode="processor",
            body=body,
            event_id=event_id,
            context=hook.context,
            message_ids=message_ids,
        )
    except Exception:
        logger.exception("hook processor payload failed hook=%s event=%s", hook.id, event_id)
        return
    for attempt, delay in enumerate(_delays):
        if attempt:
            _sleep(delay)
            current = hooks_store.get(hook.id)
            if current is None or not current.enabled:
                return  # deleted or disabled while waiting: takes effect now
            hook = current
        raw, headers = request_for(hook, payload)
        started = time.monotonic()
        err: HookError | None = None
        try:
            resp = get_client().post(
                hook.url, content=raw, headers=headers, timeout=PROCESSOR_TIMEOUT_S
            )
            if not 200 <= resp.status_code < 300:
                err = HookError(f"http {resp.status_code}")
        except httpx.TimeoutException:
            err = HookError("timeout", timeout=True)
        except httpx.HTTPError as exc:
            err = HookError(f"transport {type(exc).__name__}")
        logger.info(
            "hook call hook=%s event=%s mode=processor attempt=%d ms=%d ok=%d",
            hook.id,
            event_id,
            attempt + 1,
            int((time.monotonic() - started) * 1000),
            int(err is None),
        )
        if err is None:
            hooks_store.record_call(hook.id, ok=True)
            return
        _note_error(hook, err)
    hooks_store.record_dropped(hook.id)
    logger.warning("hook processor dropped hook=%s event=%s", hook.id, event_id)


def _dedup_key(*parts: str) -> str:
    return "hookflag-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def raise_flag(
    hook: Hook,
    *,
    family_ids: set[str],
    conv_key: str | None,
    subject: User | None,
    title: str,
    body: str,
) -> int:
    """A `hook_flag` alert in each family of the participants, at most one per
    hook + conversation + family per hour. Returns how many were created."""
    created = 0
    for fid in sorted(family_ids):
        if not rate_limits_store.check_and_increment(
            _dedup_key(hook.id, conv_key or "", fid),
            limit=1,
            window_s=FLAG_DEDUP_WINDOW_S,
        ):
            continue
        alert = alerts_module._base_alert("hook_flag")
        alert.update(
            {
                "status": "open",
                "subjectUid": subject.uid if subject else None,
                "subjectAlias": subject.alias if subject else None,
                "preview": body[:ALERT_BODY_MAX],
                "convKey": conv_key,
                "title": title[:ALERT_TITLE_MAX],
                "hookId": hook.id,
                "hookName": hook.name,
            }
        )
        try:
            alerts_module.create(fid, alert)
            created += 1
        except Exception:
            logger.exception("hook_flag alert failed hook=%s family=%s", hook.id, fid)
    return created


def raise_down(hook: Hook, err: str) -> None:
    """One `hook_down` alert to every super admin's family."""
    seen: set[str] = set()
    for u in users_store.list_users():
        if u.role != "super" or not u.familyId or u.familyId in seen:
            continue
        seen.add(u.familyId)
        alert = alerts_module._base_alert("hook_down")
        alert.update(
            {
                "status": "open",
                "preview": f"{hook.name}: {err}"[:ALERT_BODY_MAX],
                "title": f"Hook {hook.name} is failing"[:ALERT_TITLE_MAX],
                "hookId": hook.id,
                "hookName": hook.name,
            }
        )
        try:
            alerts_module.create(u.familyId, alert)
        except Exception:
            logger.exception("hook_down alert failed hook=%s", hook.id)
