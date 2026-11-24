# ruff: noqa: F811
"""Top-up policy, requests and settlement, and their sweep clauses."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import jobs, topups
from app.carriers.usmobile import SERVICE_NUMBER
from app.db.firestore import get_db
from app.store import bridge_outbox, carrier_log
from app.store import bridges as bridges_store
from app.store import families as families_store
from app.store import topups as topups_store
from app.store import users as users_store
from tests.bridge_world import (  # noqa: F401
    RecordingFCM,
    World,
    broker,
    client,
    fcm,
    make_pager_device,
    pair_bridge,
    world,
)
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header
from tests.test_carriers import Env


@pytest.fixture
def env(client: TestClient, world: World, broker: FakeBrokerClient) -> Env:
    return Env(client, world, broker)


def _policy(world: World, mode: str, per_month: int | None = None) -> None:
    topup: dict = {"mode": mode}
    if per_month is not None:
        topup["perMonth"] = per_month
    # Straight to the store: the PATCH route is covered by test_notices.
    families_store.update_family(
        world.family_id,
        name=None,
        settings=families_store.FamilySettingsPatch.model_validate({"topup": topup}),
    )


def _press(env: Env, headers: dict | None = None, pack: str = "text100"):
    return env.client.post(
        f"/api/family/bridges/{env.bridge.id}/topup",
        json={"pack": pack},
        headers=headers or env.world.admin_headers,
    )


def _settle(env: Env, topup_id: str, state: str = "confirmed") -> None:
    topups_store.set_state(env.bridge.id, topup_id, state, only_from=("requested", "unconfirmed"))


def _get(env: Env, headers: dict | None = None) -> dict:
    r = env.client.get(f"/api/family/bridges/{env.bridge.id}/topups", headers=headers or env.world.admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_off_by_default_refuses_and_says_why(env: Env):
    body = _get(env)
    assert body["mode"] == "off" and body["allowed"] is False
    assert body["reason"] == "top-ups are off for this family"
    r = _press(env)
    assert r.status_code == 403 and r.json()["detail"] == "top-ups are off for this family"
    assert topups_store.list_recent(env.bridge.id) == []
    assert [i for i in bridge_outbox.list_pending(env.bridge.id) if i.id.startswith("ob_cr_tu_")] == []


def test_a_press_queues_the_carrier_command_and_records_the_request(env: Env):
    _policy(env.world, "unlimited")
    r = _press(env)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["pack"] == "text100" and out["texts"] == 100 and out["state"] == "requested"
    assert out["by"] == "adm"
    (item,) = [i for i in bridge_outbox.list_pending(env.bridge.id) if i.id.startswith("ob_cr_tu_")]
    assert item.id == f"ob_cr_tu_{out['id']}"
    assert (item.kind, item.source, item.to, item.text) == (
        "send", "sms", {"phone": SERVICE_NUMBER}, "Top-up text 100",
    )
    assert item.msgId is None and item.bid is None
    row = topups_store.get(env.bridge.id, out["id"])
    assert row is not None and row.outboxId == item.id
    kinds = [(r.dir, r.kind) for r in carrier_log.list_recent(env.bridge.id)]
    assert ("out", "topup") in kinds


def test_limited_boundary_at_n_and_failed_does_not_count(env: Env):
    _policy(env.world, "limited", 2)
    first = _press(env).json()
    assert _press(env).status_code == 409  # one waits for its reply at a time
    _settle(env, first["id"], "failed")  # does not count
    body = _get(env)
    assert body["used"] == 0 and body["allowed"] is True and body["perMonth"] == 2
    a = _press(env).json()
    _settle(env, a["id"])
    b = _press(env)
    assert b.status_code == 201
    _settle(env, b.json()["id"])
    body = _get(env)
    assert body["used"] == 2 and body["allowed"] is False and body["reason"] == "2 of 2 used this month"
    r = _press(env)
    assert r.status_code == 403 and r.json()["detail"] == "2 of 2 used this month"
    assert [h["state"] for h in body["history"]] == ["confirmed", "confirmed", "failed"]


def test_unlimited_has_no_count_limit_but_never_two_in_flight(env: Env):
    _policy(env.world, "unlimited")
    for _ in range(4):
        r = _press(env)
        assert r.status_code == 201
        assert _press(env).status_code == 409
        _settle(env, r.json()["id"])
    assert _get(env)["used"] == 4 and _get(env)["allowed"] is True


def test_the_carriers_reply_settles_the_request(env: Env):
    _policy(env.world, "limited", 3)
    t = _press(env).json()
    env.sms_event("Top-up successful! 100 texts have been added. You now have 145 texts remaining.")
    assert topups_store.get(env.bridge.id, t["id"]).state == "confirmed"
    assert env.left() == 145
    t2 = _press(env).json()
    env.sms_event("Top-up failed: payment was declined.")
    row = topups_store.get(env.bridge.id, t2["id"])
    assert row.state == "failed" and "declined" in (row.note or "")
    assert _get(env)["used"] == 1


def test_a_command_that_could_not_be_sent_is_failed_not_counted(env: Env):
    _policy(env.world, "limited", 1)
    t = _press(env).json()
    r = env.client.post(
        f"/bridge/outbox/ob_cr_tu_{t['id']}/ack",
        json={"state": "failed", "reason": "sms_generic", "tier": 1},
        headers=env.headers,
    )
    assert r.status_code == 204
    assert topups_store.get(env.bridge.id, t["id"]).state == "failed"
    assert _get(env)["used"] == 0 and _press(env).status_code == 201


def test_no_reply_in_fifteen_minutes_is_unconfirmed_and_counts(env: Env):
    _policy(env.world, "limited", 1)
    t = _press(env).json()
    now = int(time.time())
    # Still waiting for the phone: nothing to expire however long it takes.
    assert topups.expire_unconfirmed(env.bridge.id, now=now + 3600) == 0
    bridge_outbox.ack(env.bridge.id, f"ob_cr_tu_{t['id']}", "sent", None, 1)
    assert topups.expire_unconfirmed(env.bridge.id, now=now + 600) == 0
    assert topups.expire_unconfirmed(env.bridge.id, now=now + 16 * 60) == 1
    assert topups_store.get(env.bridge.id, t["id"]).state == "unconfirmed"
    body = _get(env)
    assert body["used"] == 1 and body["reason"] == "1 of 1 used this month"
    assert _press(env).status_code == 403
    # A late reply still settles it.
    env.sms_event("Top-up successful! 100 texts have been added.")
    assert topups_store.get(env.bridge.id, t["id"]).state == "confirmed"


def test_tick_expires_unconfirmed_ones(env: Env):
    _policy(env.world, "unlimited")
    t = _press(env).json()
    bridge_outbox.ack(env.bridge.id, f"ob_cr_tu_{t['id']}", "sent", None, 1)
    from app.carriers import service

    service.tick(now=int(time.time()) + 20 * 60, broker=env.broker)
    assert topups_store.get(env.bridge.id, t["id"]).state == "unconfirmed"


def test_a_confirmed_topup_raises_the_estimate_and_announces_it(env: Env):
    from tests.test_bridge_health import _texts

    _policy(env.world, "unlimited")
    env.sms_event("You have 3 texts remaining.")  # low
    env.broker.clear()
    _press(env)
    env.sms_event("Top-up complete. 100 texts added.")
    assert env.left() == 103 and env.carrier().level == "ok" and env.carrier().checkDue is True
    assert _texts(env.broker) == ["Bridge phone texts topped up. 103 left."]


def test_the_limit_rolls_over_at_local_midnight_not_utc(env: Env, monkeypatch):
    monkeypatch.setenv("RELAY_TIMEZONE", "America/Los_Angeles")
    _policy(env.world, "limited", 1)
    bridge = env.fresh()
    nov = datetime(2026, 12, 1, 7, 30, tzinfo=UTC)  # still November in Los Angeles
    first = topups.request(bridge, "text100", "adm", now=nov)
    _settle(env, first.id)
    with pytest.raises(topups.Refused) as exc:
        topups.request(bridge, "text100", "adm", now=datetime(2026, 12, 1, 7, 59, tzinfo=UTC))
    assert exc.value.status == 403 and exc.value.detail == "1 of 1 used this month"
    # 08:00 UTC is 00:00 on 1 Dec in Los Angeles: a new month, a new allowance.
    second = topups.request(bridge, "text100", "adm", now=datetime(2026, 12, 1, 8, 0, tzinfo=UTC))
    assert second.state == "requested"
    # Under UTC the first one was already December's.
    _settle(env, second.id)
    monkeypatch.setenv("RELAY_TIMEZONE", "UTC")
    with pytest.raises(topups.Refused):
        topups.request(bridge, "text100", "adm", now=datetime(2026, 12, 2, tzinfo=UTC))


def _race(env: Env, n: int) -> list[str]:
    bridge = env.fresh()
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def press() -> None:
        barrier.wait()
        try:
            topups.request(bridge, "text100", "adm")
            outcome = "ok"
        except topups.Refused as exc:
            outcome = str(exc.status)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=press) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.mark.parametrize(
    ("mode", "per_month", "used", "rows", "conflicts"),
    [
        pytest.param("limited", 3, 2, 3, None, id="limited-cannot-exceed-the-limit"),
        pytest.param("unlimited", None, 0, None, 7, id="unlimited-buys-once"),
    ],
)
def test_concurrent_presses_admit_exactly_one(
    env: Env, mode: str, per_month: int | None, used: int, rows: int | None, conflicts: int | None
):
    _policy(env.world, mode, per_month)
    now = datetime.now(UTC)
    for i in range(used):
        get_db().collection("bridges").document(env.bridge.id).collection("topups").document(f"old{i}").set(
            {"pack": "text100", "packLabel": "100 texts", "texts": 100, "by": "adm", "at": now,
             "state": "confirmed", "outboxId": None, "note": None}
        )
    results = _race(env, 8)
    assert results.count("ok") == 1, results
    if rows is not None:
        assert len(topups_store.list_recent(env.bridge.id)) == rows
    if conflicts is not None:
        assert results.count("409") == conflicts, results


def test_the_owner_may_press_other_members_and_families_may_not(env: Env, world: World):
    _policy(world, "unlimited")
    users_store.create_user(uid="sis", alias="sis", display_name="Sis", family_id=world.family_id)
    # a family-scoped route reads role/fam from the token's claims, so on the Auth emulator the
    # two plain members need accounts with claims (the home backend reads the user document)
    for uid in ("kid", "sis"):
        fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
        fb_auth.set_custom_user_claims(uid, {"role": "member", "fam": world.family_id})
    owner = auth_header("kid")
    assert _get(env, owner)["mode"] == "unlimited"
    r = _press(env, owner)
    assert r.status_code == 201 and r.json()["by"] == "kid"
    assert _press(env, auth_header("sis")).status_code == 403
    assert env.client.get(
        f"/api/family/bridges/{env.bridge.id}/topups", headers=auth_header("sis")
    ).status_code == 403
    other = families_store.create_family(name="Other", created_by="root")
    fb_auth.create_user(uid="adm2", email="adm2@example.com")
    users_store.create_user(uid="adm2", alias="adm2", display_name="a2", role="admin", family_id=other.id)
    fb_auth.set_custom_user_claims("adm2", {"role": "admin", "fam": other.id})
    assert _press(env, auth_header("adm2")).status_code == 404
    assert env.client.post(
        f"/api/family/bridges/{env.bridge.id}/topup", json={"pack": "text100"}
    ).status_code in (401, 403)


def test_unknown_pack_and_a_carrier_without_packs(env: Env, world: World):
    _policy(world, "unlimited")
    assert _press(env, pack="nope").status_code == 404
    from app.carriers import service

    service.set_carrier(env.fresh(), "generic")
    body = _get(env)
    assert body["allowed"] is False and "has no top-up" in body["reason"] and body["packs"] == []
    assert _press(env).status_code == 404


def test_an_unpaired_bridge_cannot_top_up(env: Env, world: World):
    _policy(world, "unlimited")
    bridges_store.unpair(env.bridge.id)
    assert _press(env).status_code == 409


def test_sweep_keeps_topups_400_days(env: Env):
    db = get_db()
    now = datetime.now(UTC)
    topups_col = db.collection("bridges").document(env.bridge.id).collection("topups")
    base = {"pack": "text100", "packLabel": "", "texts": 100, "by": "adm", "state": "confirmed"}
    topups_col.document("old").set({**base, "at": now - timedelta(days=401)})
    topups_col.document("new").set({**base, "at": now - timedelta(days=399)})
    result = jobs.sweep()
    assert result.topupsDeleted == 1
    assert {s.id for s in topups_col.stream()} == {"new"}


def test_a_member_lists_only_their_own_bridges(env: Env, world: World):
    users_store.create_user(uid="sis", alias="sis", display_name="Sis", family_id=world.family_id)
    for uid in ("kid", "sis"):
        fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
        fb_auth.set_custom_user_claims(uid, {"role": "member", "fam": world.family_id})
    got = env.client.get("/api/me/bridges", headers=auth_header("kid"))
    assert got.status_code == 200, got.text
    (mine,) = got.json()
    assert mine["id"] == env.bridge.id and mine["label"] == env.bridge.label
    assert set(mine) == {"id", "label", "paired", "health", "carrier"}
    assert mine["paired"] is True and mine["carrier"]["canTopup"] is not None
    # nobody else's: another member, and the family admin who is not the owner
    assert env.client.get("/api/me/bridges", headers=auth_header("sis")).json() == []
    assert env.client.get("/api/me/bridges", headers=world.admin_headers).json() == []
    assert env.client.get("/api/me/bridges").status_code in (401, 403)
