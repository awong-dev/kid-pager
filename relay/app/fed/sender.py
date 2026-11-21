"""The federation sender: posts pending `fedOutbox` rows to their link in order, with backoff,
`peer_offline` expiry and link-health notices. Configured once at startup via `configure`."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from app import deliveries, notices
from app import hooks as hooks_module
from app.broker import BrokerClient
from app.config import Settings
from app.fed import client as client_mod
from app.fed import moved
from app.fed.client import FedClient, FedError, InboxResult
from app.store import externals as externals_store
from app.store import fed_families, fed_links, fed_outbox, fed_state
from app.store.fed_links import FedLink
from app.store.fed_outbox import OutboxRow

logger = logging.getLogger("relay.fed.sender")

BACKOFF_S = (5.0, 30.0, 120.0)
DEFAULT_OFFLINE_AFTER_S = 24 * 3600
DEFAULT_DOWN_AFTER_S = 15 * 60
PING_EVERY_S = 3600
MAX_BATCHES_PER_FLUSH = 10
RSN_STORE_MAX = 32
NOTICE_MAX_BYTES = 90

_settings: Settings | None = None
_client: FedClient | None = None
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def configure(settings: Settings | None, client: FedClient | None = None) -> None:
    """Startup (and tests): the settings the relay's own address is derived from, and
    optionally the client to use (default: `client.get_client()`)."""
    global _settings, _client
    _settings = settings
    _client = client


def enabled() -> bool:
    """Configured and the server switch is on."""
    return _settings is not None and fed_state.server_enabled()


def settings() -> Settings:
    assert _settings is not None
    return _settings


def _get_client() -> FedClient:
    return _client if _client is not None else client_mod.get_client()


def offline_after_s() -> int:
    return _env_int("FED_OFFLINE_AFTER_S", DEFAULT_OFFLINE_AFTER_S)


def down_after_s() -> int:
    return _env_int("FED_DOWN_AFTER_S", DEFAULT_DOWN_AFTER_S)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _epoch(ts: datetime | None) -> int | None:
    return int(ts.timestamp()) if ts is not None else None


def _created(row: OutboxRow) -> int:
    return _epoch(row.createdAt) or 0


def live(link: FedLink) -> bool:
    """The link may carry traffic now: active, its family effective."""
    return link.state == "active" and fed_state.family_enabled(link.familyId)


def kick(link_id: str) -> None:
    """Posts the link's pending rows in the background (the hooks seam), without delaying the
    request that queued one. A no-op when federation is off."""
    if not enabled():
        return
    hooks_module.spawn(lambda: _flush_quietly(link_id))


def _flush_quietly(link_id: str) -> None:
    try:
        flush_link(link_id)
    except Exception:
        logger.exception("fed flush failed link=%s", link_id)


@dataclass
class FlushResult:
    sent: int = 0
    failed: int = 0
    retried: int = 0
    error: str | None = None


def _lock_for(link_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(link_id, threading.Lock())


def backoff_for(attempts: int) -> float:
    """Seconds to wait after the `attempts`-th failed try: 5, 30, 120, then 0 (due again on the
    next tick)."""
    return BACKOFF_S[attempts - 1] if 1 <= attempts <= len(BACKOFF_S) else 0.0


def flush_link(link_id: str, *, now: float | None = None) -> FlushResult:
    """Posts the due rows of `link_id`, oldest first, <= 20 per request, until none is due or a
    request fails. One flush per link at a time (a second concurrent call returns at once: the
    first one will see its rows). A pending `moved` goes first."""
    result = FlushResult()
    if not enabled():
        return result
    lock = _lock_for(link_id)
    if not lock.acquire(blocking=False):
        return result
    try:
        link = fed_links.get(link_id)
        if link is None or not live(link):
            return result
        identity = fed_families.get(link.familyId)
        if identity is None:
            return result
        t = time.time() if now is None else now
        if link.announce_pending:
            if not moved.announce(settings(), _get_client(), identity, link, now=t):
                return result
            link = fed_links.get(link_id)
            if link is None:
                return result
        client = _get_client()
        for _ in range(MAX_BATCHES_PER_FLUSH):
            t = time.time() if now is None else now
            batch = fed_outbox.due_batch(link_id, now=t)
            if not batch:
                break
            try:
                answers = client.post_inbox(identity, link, [row.event for row in batch])
            except FedError as exc:
                _batch_failed(link_id, batch, t, str(exc), result)
                break
            fed_links.record_ok(link_id)
            _apply_results(batch, answers, t, result)
            if len(batch) < fed_outbox.BATCH_MAX:
                break
    finally:
        lock.release()
    return result


def _batch_failed(
    link_id: str, batch: list[OutboxRow], now: float, error: str, result: FlushResult
) -> None:
    fed_links.record_err(link_id)
    result.error = error
    for row in batch:
        fed_outbox.record_attempt(
            row.id, now + backoff_for(row.attempts + 1), error[:128]
        )
        result.retried += 1
    logger.info("fed post failed link=%s rows=%d err=%s", link_id, len(batch), error)


def _apply_results(
    batch: list[OutboxRow], answers: list[InboxResult], now: float, result: FlushResult
) -> None:
    by_id = {a.id: a for a in answers}
    for row in batch:
        event_id = str(row.event.get("id") or "")
        answer = by_id.get(event_id)
        if answer is None:
            # The other relay left this event out of its answer: keep it, try again.
            fed_outbox.record_attempt(
                row.id, now + backoff_for(row.attempts + 1), "no_result"
            )
            result.retried += 1
            continue
        if answer.st in ("ok", "dup"):
            fed_outbox.mark_acked(row.id)
            if row.msgId:
                deliveries.mark_sent(row.msgId, "fed", kind="fed")
            result.sent += 1
        else:
            # `rej` -- and any state this relay does not know, which is a refusal
            # the sender must see rather than a silent retry.
            reason = (answer.rsn or "rejected") if answer.st == "rej" else "bad_reply"
            reason = reason[:RSN_STORE_MAX]
            fed_outbox.mark_failed(row.id, reason)
            if row.msgId:
                deliveries.mark_failed(row.msgId, "fed", error=reason, kind="fed")
            result.failed += 1


def fail_link_rows(link_id: str, error: str) -> int:
    """Fails every pending row of the link (and the deliveries they stood for)."""
    rows = fed_outbox.fail_pending_for_link(link_id, error)
    for row in rows:
        if row.msgId:
            deliveries.mark_failed(row.msgId, "fed", error=error, kind="fed")
    return len(rows)


def fail_offline(*, now: float | None = None) -> int:
    """Rows pending longer than `FED_OFFLINE_AFTER_S` fail `peer_offline`, and their deliveries
    with them (a `contact` row has no delivery)."""
    t = time.time() if now is None else now
    cutoff = t - offline_after_s()
    failed = 0
    for row in fed_outbox.list_pending():
        if _created(row) > cutoff:
            continue
        fed_outbox.mark_failed(row.id, "peer_offline")
        if row.msgId:
            deliveries.mark_failed(row.msgId, "fed", error="peer_offline", kind="fed")
        failed += 1
    return failed


def retry_pass(*, now: float | None = None) -> int:
    """Flushes every live link that has a pending row or a pending `moved`. Returns the rows
    sent."""
    sent = 0
    for link in fed_links.list_all():
        if not live(link):
            continue
        if not link.announce_pending and not fed_outbox.list_pending(link.id):
            continue
        try:
            sent += flush_link(link.id, now=now).sent
        except Exception:
            logger.exception("fed retry failed link=%s", link.id)
    return sent


def announce_pass(*, now: float | None = None) -> int:
    """A link with a pending `moved` and nothing else to send is still announced (a quiet link
    would never be flushed otherwise). Returns the announcements accepted."""
    done = 0
    for link in fed_links.list_all():
        if link.announce_pending and live(link) and not fed_outbox.list_pending(link.id):
            try:
                flush_link(link.id, now=now)
            except Exception:
                logger.exception("fed announce failed link=%s", link.id)
            after = fed_links.get(link.id)
            if after is not None and not after.announce_pending:
                done += 1
    return done


def tick_pass(*, now: float | None = None, broker: BrokerClient | None = None) -> None:
    """`app/jobs.py` `tick()`'s federation work: retry, offline, health, idle ping. Each step is
    isolated; a failing one is logged and does not stop the others."""
    # With the server switch off nothing is delivered, probed or announced, but rows already
    # queued still age out to `peer_offline`.
    steps = [("offline", lambda: fail_offline(now=now))]
    if enabled():
        steps = [
            ("retry", lambda: retry_pass(now=now)),
            ("announce", lambda: announce_pass(now=now)),
            *steps,
            ("ping", lambda: ping_pass(now=now)),
            ("health", lambda: health_pass(now=now, broker=broker)),
        ]
    for name, step in steps:
        try:
            step()
        except Exception:
            logger.exception("fed tick step %s failed", name)


def ping_pass(*, now: float | None = None) -> int:
    """Pings a live link with nothing pending when its last 2xx is over an hour old (never
    heard: also, when it has people on it), or when it is down (every tick, so the end of an
    outage is noticed). Returns the successful pings."""
    t = time.time() if now is None else now
    ok = 0
    for link in fed_links.list_all():
        if not live(link) or link.announce_pending or fed_outbox.list_pending(link.id):
            continue
        last = _epoch(link.lastOkAt)
        if link.health.state == "ok" and last is not None and t - last < PING_EVERY_S:
            continue
        if link.health.state == "ok" and last is None and not _externals(link):
            continue
        identity = fed_families.get(link.familyId)
        if identity is None:
            continue
        try:
            answers = _get_client().post_inbox(
                identity, link, [{"type": "ping", "id": f"pg_{int(t)}"}]
            )
        except FedError as exc:
            fed_links.record_err(link.id)
            logger.info("fed ping failed link=%s err=%s", link.id, exc)
            continue
        if any(a.st in ("ok", "dup") for a in answers):
            fed_links.record_ok(link.id)
            ok += 1
    return ok


def _externals(link: FedLink):
    return externals_store.list_fed_externals(link.familyId, link.remote.fedId)


def down_text(name: str, aliases: list[str]) -> str:
    """`<name> unreachable. Messages to <aliases> are waiting.`, <= 90 bytes: the alias list is
    cut (`a, b, +2`) to fit; without room for even one alias, or without any, the short form."""
    head = f"{name} unreachable."
    if not aliases:
        return notices_clip(head)
    for keep in range(len(aliases), 0, -1):
        names = ", ".join(aliases[:keep])
        if keep < len(aliases):
            names += f", +{len(aliases) - keep}"
        text = f"{head} Messages to {names} are waiting."
        if len(text.encode("utf-8")) <= NOTICE_MAX_BYTES:
            return text
    return notices_clip(head)


def up_text(name: str) -> str:
    return notices_clip(f"{name} is back.")


def notices_clip(text: str) -> str:
    out = text
    while len(out.encode("utf-8")) > NOTICE_MAX_BYTES:
        out = out[:-1]
    return out


def _waiting_aliases(link: FedLink) -> list[str]:
    """The aliases (sorted, unique) of the people on `link` with a message pending."""
    names: set[str] = set()
    people = {(u.fed or {}).get("rid"): u for u in _externals(link)}
    for row in fed_outbox.list_pending(link.id):
        if not row.msgId:
            continue
        user = people.get(row.event.get("to"))
        if user is not None:
            names.add(user.alias)
    return sorted(names)


def health_pass(
    *, now: float | None = None, broker: BrokerClient | None = None
) -> None:
    """Computes each live link's health, announces the edges (`peer_down` / `peer_up`), and
    stores it when it changed. A notice that cannot be written leaves the stored state alone, so
    the next tick retries the edge."""
    t = int(time.time() if now is None else now)
    for link in fed_links.list_all():
        if not live(link):
            continue
        try:
            _evaluate(link, t, broker)
        except Exception:
            logger.exception("fed health failed link=%s", link.id)


def _evaluate(link: FedLink, now: int, broker: BrokerClient | None) -> None:
    old = notices.Health(
        state=link.health.state,
        since=link.health.since,
        notifiedAt=link.health.notifiedAt,
    )
    pending = fed_outbox.list_pending(link.id)
    last_ok = _epoch(link.lastOkAt) or 0
    down = False
    if pending:
        waiting_since = _created(pending[0])
        # No 2xx since the oldest pending row was queued, for the whole window.
        down = last_ok <= waiting_since and now - waiting_since >= down_after_s()
    if old.state != "ok":
        # An outage ends on a 2xx from the other relay after it began; rows that merely aged out
        # (`peer_offline`) do not end it.
        down = down or last_ok < old.since
    if down:
        new, announce = notices.enter_bad(old, "down", now)
        if announce:
            text = down_text(link.remote.name, _waiting_aliases(link))
            notices.notify(
                link.familyId, "peer_down", text, key=link.id, since=new.since, broker=broker
            )
    else:
        new, announce = notices.enter_ok(old, max(last_ok, old.since))
        if announce:
            notices.notify(
                link.familyId,
                "peer_up",
                up_text(link.remote.name),
                key=link.id,
                resolves="peer_down",
                since=max(last_ok, old.since),
                broker=broker,
            )
    if new != old:
        fed_links.set_health(
            link.id, state=new.state, since=new.since, notifiedAt=new.notifiedAt
        )


def send_unlink(identity: fed_families.FedFamily, link: FedLink) -> bool:
    """`POST /fed/v1/unlink`, signed. Best effort: a failure is logged and ignored (the other side
    sees 403s once it is gone from here). Returns whether it was acknowledged."""
    try:
        reply = _get_client().link_call(
            identity, link, "POST", f"{client_mod.API}/unlink", json.dumps({}).encode()
        )
    except FedError as exc:
        logger.info("fed unlink not delivered link=%s err=%s", link.id, exc)
        return False
    return reply.status == 200


def unlink_in_background(identity: fed_families.FedFamily, link: FedLink) -> None:
    if not enabled():
        return
    hooks_module.spawn(lambda: send_unlink(identity, link))
