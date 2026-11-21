"""Day-based retention settings and the sweep classes; rows are backdated through `get_db()`."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from app import jobs
from app.db.firestore import get_db
from app.store import settings as settings_store
from app.store.settings import RetentionSetting


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _exists(path: str) -> bool:
    parts = path.split("/")
    ref = get_db().collection(parts[0]).document(parts[1])
    for i in range(2, len(parts), 2):
        ref = ref.collection(parts[i]).document(parts[i + 1])
    return ref.get().exists


def test_defaults_are_in_days():
    r = settings_store.get_retention()
    assert (r.messagesDays, r.locationsDays, r.alertsDays, r.batteryDays) == (
        28,
        7,
        90,
        90,
    )


def test_legacy_week_fields_are_read_as_days_and_dropped_on_save():
    get_db().collection("settings").document("retention").set(
        {"messages": {"n": 2, "unit": "weeks"}, "locations": {"n": 3, "unit": "days"}}
    )
    r = settings_store.get_retention()
    assert (r.messagesDays, r.locationsDays) == (14, 3)
    settings_store.set_retention(alertsDays=30)
    raw = get_db().collection("settings").document("retention").get().to_dict()
    assert raw == {
        "messagesDays": 14,
        "locationsDays": 3,
        "alertsDays": 30,
        "batteryDays": 90,
    }


def test_a_stored_location_window_above_the_ceiling_is_clamped_on_read():
    get_db().collection("settings").document("retention").set(
        {"locations": {"n": 52, "unit": "weeks"}, "alertsDays": 1}
    )
    r = settings_store.get_retention()
    assert r.locationsDays == 30
    assert r.alertsDays == 7


def test_set_retention_merges_and_rejects_out_of_bounds():
    settings_store.set_retention(messagesDays=10)
    settings_store.set_retention(locationsDays=2)
    r = settings_store.get_retention()
    assert (r.messagesDays, r.locationsDays) == (10, 2)
    for kw in ({"locationsDays": 31}, {"messagesDays": 0}, {"batteryDays": 6}):
        try:
            settings_store.set_retention(**kw)
        except ValueError:
            continue
        raise AssertionError(kw)
    assert settings_store.get_retention().locationsDays == 2


def test_sms_log_swept_by_message_window():
    settings_store.set_retention(messagesDays=10)
    col = get_db().collection("devices").document("d1").collection("smsLog")
    col.document("old").set({"body": "secret", "peer": "+1555", "receivedAt": _ago(11)})
    col.document("new").set({"body": "fresh", "peer": "+1555", "receivedAt": _ago(9)})
    res = jobs.sweep()
    assert res.smsLogDeleted == 1
    assert not _exists("devices/d1/smsLog/old")
    assert _exists("devices/d1/smsLog/new")


def test_rate_limits_older_than_a_day_swept():
    now = time.time()
    col = get_db().collection("rateLimits")
    col.document("ip:1.2.3.4").set({"windowStart": now - 2 * 86400, "count": 1})
    col.document("ip:5.6.7.8").set({"windowStart": now - 3600, "count": 1})
    res = jobs.sweep()
    assert res.rateLimitsDeleted == 1
    assert not _exists("rateLimits/ip:1.2.3.4")
    assert _exists("rateLimits/ip:5.6.7.8")


def test_cells_resolved_over_30_days_ago_swept():
    col = get_db().collection("cells")
    col.document("old").set({"lat": 1.0, "lon": 2.0, "resolvedAt": _ago(31)})
    col.document("new").set({"lat": 1.0, "lon": 2.0, "resolvedAt": _ago(29)})
    res = jobs.sweep()
    assert res.cellsDeleted == 1
    assert not _exists("cells/old")
    assert _exists("cells/new")


def test_contact_requests_90_days():
    col = get_db().collection("contactRequests")
    col.document("old").set({"status": "added", "createdAt": _ago(91)})
    col.document("new").set({"status": "added", "createdAt": _ago(89)})
    res = jobs.sweep()
    assert res.contactRequestsDeleted == 1
    assert not _exists("contactRequests/old")
    assert _exists("contactRequests/new")


def test_expired_setup_codes_swept():
    col = get_db().collection("setupCodes")
    col.document("gone").set({"deviceId": "d", "expiresAt": _ago(0.1)})
    col.document("live").set(
        {"deviceId": "d", "expiresAt": datetime.now(UTC) + timedelta(hours=1)}
    )
    res = jobs.sweep()
    assert res.setupCodesDeleted == 1
    assert not _exists("setupCodes/gone")
    assert _exists("setupCodes/live")


def test_open_alerts_expire_with_their_held_body():
    settings_store.set_retention(alertsDays=30)
    col = get_db().collection("families").document("f1").collection("alerts")
    col.document("open_old").set(
        {"status": "open", "kind": "sms_unknown", "heldBody": "hello", "ts": _ago(31)}
    )
    col.document("open_new").set(
        {"status": "open", "kind": "sms_unknown", "ts": _ago(29)}
    )
    col.document("done_old").set(
        {"status": "handled", "kind": "sms_unknown", "ts": _ago(31)}
    )
    res = jobs.sweep()
    assert res.alertsDeleted == 2
    assert not _exists("families/f1/alerts/open_old")
    assert not _exists("families/f1/alerts/done_old")
    assert _exists("families/f1/alerts/open_new")


def _loc_message(mid: str, days: float) -> None:
    get_db().collection("messages").document(mid).set(
        {
            "seq": 1,
            "convKey": "a_b",
            "uids": ["a", "b"],
            "senderUid": "a",
            "recipientUid": "b",
            "kind": "loc",
            "body": "47.6,-122.3",
            "loc": {"lat": 47.6, "lon": -122.3},
            "ts": 1,
            "createdAt": _ago(days),
            "deliveries": {},
            "pendingDeviceIds": [],
        }
    )


def test_loc_field_scrubbed_at_location_window_message_kept():
    settings_store.set_retention(messagesDays=28, locationsDays=7)
    _loc_message("m_old", 8)
    _loc_message("m_new", 6)
    res = jobs.sweep()
    assert res.locMessagesScrubbed == 1
    old = get_db().collection("messages").document("m_old").get().to_dict()
    assert old["loc"] is None
    assert old["body"] == "location (expired)"
    assert old["locExpired"] is True
    new = get_db().collection("messages").document("m_new").get().to_dict()
    assert new["loc"] == {"lat": 47.6, "lon": -122.3}
    # idempotent
    assert jobs.sweep().locMessagesScrubbed == 0


def test_device_status_last_cell_scrubbed_when_older_than_location_limit():
    settings_store.set_retention(locationsDays=7)
    now = int(time.time())
    for did, age_days in (("d_old", 8), ("d_new", 1)):
        get_db().collection("devices").document(did).set(
            {
                "familyId": "f",
                "status": {
                    "battMv": 3900,
                    "lastCell": {
                        "mcc": "310",
                        "mnc": "410",
                        "tac": 1,
                        "ci": 2,
                        "ts": now - age_days * 86400,
                    },
                },
            }
        )
    res = jobs.sweep()
    assert res.deviceStatusScrubbed == 1
    old = get_db().collection("devices").document("d_old").get().to_dict()
    assert "lastCell" not in old["status"]
    assert old["status"]["battMv"] == 3900
    assert (
        "lastCell"
        in get_db().collection("devices").document("d_new").get().to_dict()["status"]
    )


def test_sweep_records_last_sweep_report_with_oldest():
    settings_store.set_retention(messagesDays=10, locationsDays=3)
    surviving = _ago(2)
    get_db().collection("devices").document("d").collection("locations").document(
        "l1"
    ).set({"lat": 1.0, "lon": 2.0, "createdAt": surviving})
    get_db().collection("devices").document("d").collection("locations").document(
        "l0"
    ).set({"lat": 1.0, "lon": 2.0, "createdAt": _ago(4)})
    res = jobs.sweep()
    assert res.locationsDeleted == 1
    meta = settings_store.get_meta()
    assert meta.lastSweep is not None
    assert meta.lastSweep.counts["locationsDeleted"] == 1
    assert meta.lastSweep.oldest["messages"] is None
    oldest_loc = meta.lastSweep.oldest["locations"]
    assert oldest_loc is not None and abs((oldest_loc - surviving).total_seconds()) < 1
    assert meta.lastSweep.at is not None


def test_loc_pass_due_once_per_clock_hour():
    now = datetime(2026, 11, 20, 10, 20, tzinfo=UTC)
    assert jobs.loc_pass_due(now)
    get_db().collection("settings").document("meta").set(
        {"lastLocPassAt": datetime(2026, 11, 20, 10, 2, tzinfo=UTC)}, merge=True
    )
    assert not jobs.loc_pass_due(now)
    assert jobs.loc_pass_due(now + timedelta(hours=1))


def test_hourly_location_pass_removes_expired_fix_without_a_full_sweep():
    settings_store.set_retention(locationsDays=1)
    get_db().collection("devices").document("d").collection("locations").document(
        "old"
    ).set({"lat": 1.0, "lon": 2.0, "createdAt": _ago(2)})
    get_db().collection("messages").document("m_text").set(
        {"kind": "text", "createdAt": _ago(2), "uids": ["a", "b"]}
    )
    counts = jobs.location_pass()
    assert counts["locationsDeleted"] == 1
    assert _exists("messages/m_text")  # the content class waits for the daily sweep
    assert settings_store.get_meta().lastSweepAt is None
    assert settings_store.get_meta().lastLocPassAt is not None


def test_legacy_retention_setting_class_still_converts():
    assert RetentionSetting(n=2, unit="weeks").days() == 14


def test_sweep_leaves_no_swept_body_in_the_sqlite_file_or_wal():
    """A message swept for age is gone from the raw database and WAL bytes (home store only)."""
    import os

    import pytest

    db = get_db()
    if not hasattr(type(db), "reclaim_space"):
        pytest.skip("Firestore has no file to inspect")
    settings_store.set_retention(messagesDays=10)
    needle = "SWEPT-BODY-" + "q" * 400
    db.collection("messages").document("m_swept").set(
        {"kind": "text", "body": needle, "uids": ["a", "b"], "createdAt": _ago(20)}
    )
    db.collection("messages").document("m_kept").set(
        {
            "kind": "text",
            "body": "KEPT-BODY-zz",
            "uids": ["a", "b"],
            "createdAt": _ago(1),
        }
    )
    jobs.sweep()
    raw = b""
    for suffix in ("", "-wal"):
        if os.path.exists(db._path + suffix):
            with open(db._path + suffix, "rb") as f:
                raw += f.read()
    assert b"SWEPT-BODY-" not in raw
    assert b"KEPT-BODY-zz" in raw


def test_cell_resolution_logs_no_coordinates(monkeypatch, caplog):
    """An implausible cell fix and a failing lookup log neither coordinates nor the exception
    text."""
    import logging

    from app import cellgeo
    from app.wire import CellInfo

    caplog.set_level(logging.INFO)
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "k")
    cell = CellInfo(mcc="310", mnc="410", tac=12345, ci=6789)

    monkeypatch.setattr(
        cellgeo,
        "_resolve_google",
        lambda c, k: cellgeo.CellFix(
            lat=47.123456, lon=-122.654321, acc_m=999_999, provider="g"
        ),
    )
    assert cellgeo.resolve(cell) is None

    def boom(c, k):
        raise RuntimeError("GET https://x/?lat=47.123456&lon=-122.654321")

    monkeypatch.setattr(cellgeo, "_resolve_google", boom)
    cell2 = CellInfo(mcc="310", mnc="410", tac=12346, ci=6790)
    assert cellgeo.resolve(cell2) is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "implausible" in text and "failed" in text
    assert "47.123456" not in text and "-122.654321" not in text
