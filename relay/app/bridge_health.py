"""Bridge liveness and capability: offline, degraded and low-battery tracks with system notices."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from app import alerts as alerts_module
from app import clockfmt, notices
from app.broker import BrokerClient
from app.store import bridges as bridges_store
from app.store.bridges import Bridge, BridgeHealth, HealthTrack

logger = logging.getLogger("relay.bridge_health")

DEFAULT_OFFLINE_AFTER_S = 15 * 60
BATTERY_LOW_PCT = 15
BATTERY_OK_PCT = 25

# What a lost flag is called in the pager text.
FLAG_WORDS = {"sms": "SMS role", "listener": "chat alerts", "accessibility": "screen access"}
CAPABILITIES = ("sms", "listener", "accessibility")


def offline_after_s() -> int:
    """Read on every call so a test or the e2e stack can shrink it (like `SWEEP_BATCH`)."""
    raw = os.environ.get("BRIDGE_OFFLINE_AFTER_S")
    try:
        value = int(raw) if raw else DEFAULT_OFFLINE_AFTER_S
    except ValueError:
        return DEFAULT_OFFLINE_AFTER_S
    return value if value > 0 else DEFAULT_OFFLINE_AFTER_S


def epoch(ts: datetime | None) -> int:
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return int(ts.timestamp())


def _ok_now(bridge: Bridge) -> dict[str, bool]:
    s = bridge.status
    return {"sms": s.smsDefault, "listener": s.listenerBound, "accessibility": s.accessibility}


def _hh(track: HealthTrack) -> notices.Health:
    return notices.Health(state=track.state, since=track.since, notifiedAt=track.notifiedAt)


def _track(h: notices.Health) -> HealthTrack:
    return HealthTrack(state=h.state, since=h.since, notifiedAt=h.notifiedAt)


def offline_text(bridge: Bridge, since: int) -> str:
    return f"Bridge phone offline since {clockfmt.clock(since, bridge.familyId)}. SMS and chat pages will wait."


def degraded_text(flags: list[str]) -> str:
    return "Bridge phone lost " + " and ".join(FLAG_WORDS[f] for f in flags) + "."


def battery_text(pct: int) -> str:
    return f"Bridge phone battery is {pct}% and not charging."


def _notify(bridge: Bridge, code: str, text: str, **kw) -> None:
    notices.notify(bridge.familyId, code, text, key=bridge.id, **kw)


def _resolve(bridge: Bridge, code: str) -> None:
    alerts_module.resolve_system(bridge.familyId, code, bridge.id)


def evaluate(
    bridge: Bridge,
    *,
    now: int | None = None,
    heartbeat: bool = False,
    broker: BrokerClient | None = None,
) -> BridgeHealth | None:
    """Recompute `bridge.health`, announce the edges, store the result; `None` if not paired."""
    if not bridge.paired:
        return None
    now_s = int(now if now is not None else time.time())
    old = bridge.health or BridgeHealth()
    h = old.model_copy(deep=True)
    last = epoch(bridge.lastSeenAt) or now_s
    offline = now_s - last > offline_after_s()

    off = _hh(h.offline)
    if offline:
        off, announce = notices.enter_bad(off, "offline", last)
        h.offline = _track(off)
        if announce:
            _notify(
                bridge, "bridge_offline", offline_text(bridge, last), since=off.since, broker=broker
            )
    elif off.state != "ok":
        off, announce = notices.enter_ok(off, last)
        h.offline = _track(off)
        if announce:
            _notify(
                bridge,
                "bridge_online",
                "Bridge phone is back.",
                resolves="bridge_offline",
                since=last,
                broker=broker,
            )
        else:
            _resolve(bridge, "bridge_offline")

    if heartbeat and not offline:
        ok_now = _ok_now(bridge)
        expect = set(h.expect) | {c for c in CAPABILITIES if ok_now[c]}
        if bridge.simNumber:
            expect.add("sms")
        h.expect = [c for c in CAPABILITIES if c in expect]
        flags = [c for c in CAPABILITIES if c in expect and not ok_now[c]]
        _step_degraded(bridge, h, flags, last, broker)
        _step_battery(bridge, h, last, broker)

    if h.offline.state == "offline":
        h.state, h.since = "offline", h.offline.since
    elif h.degraded.state == "degraded":
        h.state, h.since = "degraded", h.degraded.since
    else:
        h.state = "ok"
        h.since = max(h.offline.since, h.degraded.since) or last
    h.notifiedAt = h.offline.notifiedAt
    if bridge.health is None or h != old:
        bridges_store.set_health(bridge.id, h)
    return h


def _step_degraded(
    bridge: Bridge, h: BridgeHealth, flags: list[str], t: int, broker: BrokerClient | None
) -> None:
    tr = _hh(h.degraded)
    if not flags:
        h.flags = []
        if tr.state == "ok":
            return
        tr, announce = notices.enter_ok(tr, t)
        h.degraded = _track(tr)
        if announce:
            _notify(
                bridge,
                "bridge_online",
                "Bridge phone is fully working again.",
                resolves="bridge_degraded",
                since=t,
                broker=broker,
            )
        else:
            _resolve(bridge, "bridge_degraded")
        return
    added = set(flags) - set(h.flags)
    if tr.state == "ok":
        tr, announce = notices.enter_bad(tr, "degraded", t)
    else:
        # Still degraded; a newly lost capability is news, within the hold-off.
        announce = bool(added) and (
            tr.notifiedAt is None or t - tr.notifiedAt >= notices.HOLD_OFF_S
        )
        if announce:
            tr = notices.Health(state="degraded", since=tr.since, notifiedAt=t)
    h.degraded = _track(tr)
    h.flags = flags
    if announce:
        _notify(bridge, "bridge_degraded", degraded_text(flags), since=tr.since, broker=broker)


def _step_battery(bridge: Bridge, h: BridgeHealth, t: int, broker: BrokerClient | None) -> None:
    s = bridge.status
    tr = _hh(h.battery)
    low = s.battery is not None and s.battery <= BATTERY_LOW_PCT and s.charging is False
    recovered = s.battery is None or s.charging is True or s.battery >= BATTERY_OK_PCT
    if low and tr.state == "ok":
        tr, announce = notices.enter_bad(tr, "low", t)
        h.battery = _track(tr)
        if announce:
            _notify(bridge, "bridge_battery", battery_text(s.battery or 0), since=tr.since, broker=broker)
    elif tr.state != "ok" and recovered:
        tr, _ = notices.enter_ok(tr, t)
        h.battery = _track(tr)
        _resolve(bridge, "bridge_battery")


def forget(bridge: Bridge) -> None:
    for code in ("bridge_offline", "bridge_degraded", "bridge_battery"):
        _resolve(bridge, code)


def evaluate_all(*, now: int | None = None, broker: BrokerClient | None = None) -> int:
    """The tick's pass over every paired bridge; returns how many were evaluated."""
    count = 0
    for bridge in bridges_store.list_all():
        if not bridge.paired:
            continue
        try:
            evaluate(bridge, now=now, broker=broker)
            count += 1
        except Exception:
            logger.exception("bridge health evaluate failed bridge=%s", bridge.id)
    return count
