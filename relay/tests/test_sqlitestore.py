"""Shim-specific behaviour of app/db/sqlitestore.py; each test opens its own database."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.firestore import (
    DELETE_FIELD,
    SERVER_TIMESTAMP,
    ArrayRemove,
    ArrayUnion,
    FieldFilter,
    Increment,
    transactional,
)
from google.cloud.firestore_v1._helpers import ReadAfterWriteError

from app.db import sqlitestore


@pytest.fixture
def db(tmp_path):
    client = sqlitestore.Client(str(tmp_path / "pager.sqlite"))
    yield client
    client.close()


def _raw(db: sqlitestore.Client, path: str) -> dict:
    conn = sqlite3.connect(db._path)
    try:
        return json.loads(conn.execute("SELECT data FROM docs WHERE path = ?", (path,)).fetchone()[0])
    finally:
        conn.close()


def test_server_timestamp_resolves_to_commit_time(db):
    before = datetime.now(UTC)
    ref = db.collection("users").document("u1")
    ref.set({"createdAt": SERVER_TIMESTAMP, "nested": {"at": SERVER_TIMESTAMP}})
    data = ref.get().to_dict()
    after = datetime.now(UTC)
    assert before - timedelta(seconds=1) <= data["createdAt"] <= after
    assert data["createdAt"].tzinfo is not None
    # One commit, one timestamp.
    assert data["nested"]["at"] == data["createdAt"]


def test_server_timestamps_strictly_increase_across_commits(db):
    ref = db.collection("c").document("d")
    stamps = []
    for _ in range(5):
        ref.set({"t": SERVER_TIMESTAMP})
        stamps.append(ref.get().to_dict()["t"])
    assert stamps == sorted(stamps) and len(set(stamps)) == 5


def test_array_union_remove_increment_delete_field(db):
    ref = db.collection("c").document("d")
    ref.set({"a": [1, "x"], "n": 2, "gone": True, "keep": {"x": 1, "y": 2}})
    ref.update(
        {
            "a": ArrayUnion(["x", "y", 1]),
            "n": Increment(3),
            "missing": Increment(1),
            "gone": DELETE_FIELD,
            "keep.y": DELETE_FIELD,
        }
    )
    assert ref.get().to_dict() == {"a": [1, "x", "y"], "n": 5, "missing": 1, "keep": {"x": 1}}
    ref.update({"a": ArrayRemove([1, "nope"]), "n": Increment(0.5)})
    data = ref.get().to_dict()
    assert data["a"] == ["x", "y"]
    assert data["n"] == 5.5


def test_array_union_does_not_equate_bool_and_number(db):
    ref = db.collection("c").document("d")
    ref.set({"a": [1]})
    ref.update({"a": ArrayUnion([True])})
    assert ref.get().to_dict()["a"] == [1, True]


def test_set_merge_and_dotted_update_with_non_simple_keys(db):
    ref = db.collection("messages").document("m")
    ref.set({"deliveries": {"9f2c": {"state": "queued", "attempts": 0}}, "body": "hi"})
    # A segment that is not a simple name travels backtick-quoted in the
    # update mask (`deliveries.\`9f2c\`.state`).
    ref.update({"deliveries.9f2c.state": "sent", "deliveries.pager:pgr-1.state": "queued"})
    ref.set({"deliveries": {"9f2c": {"attempts": 1}}}, merge=True)
    assert ref.get().to_dict() == {
        "deliveries": {"9f2c": {"state": "sent", "attempts": 1}, "pager:pgr-1": {"state": "queued"}},
        "body": "hi",
    }
    ref.set({"only": 1})
    assert ref.get().to_dict() == {"only": 1}


def test_create_update_preconditions_raise_like_the_emulator(db):
    ref = db.collection("c").document("d")
    with pytest.raises(NotFound):
        ref.update({"a": 1})
    ref.create({"a": 1})
    with pytest.raises(AlreadyExists):
        ref.create({"a": 2})
    assert ref.get().to_dict() == {"a": 1}
    with pytest.raises(ValueError):
        ref.create({"a": DELETE_FIELD})  # google's own client-side check


def test_timestamp_round_trip_is_microseconds_and_tz_aware(db):
    aware = datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=UTC)
    naive = datetime(2026, 10, 31, 12, 0, 0, 1)  # noqa: DTZ001 -- naive on purpose
    ref = db.collection("c").document("d")
    ref.set({"aware": aware, "naive": naive, "list": [aware], "blob": b"\x00\xff"})
    raw = _raw(db, "c/d")
    assert raw["aware"] == {"$ts": 1793448000123456}
    assert raw["blob"] == {"$b64": "AP8="}
    data = ref.get().to_dict()
    assert data["aware"] == aware and data["aware"].tzinfo is not None
    assert data["naive"] == naive.replace(tzinfo=UTC)
    assert data["list"] == [aware]
    assert data["blob"] == b"\x00\xff"


def test_range_filter_on_timestamps_ignores_other_types(db):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    col = db.collection("locs")
    col.document("old").set({"createdAt": base})
    col.document("new").set({"createdAt": base + timedelta(days=2)})
    col.document("int").set({"createdAt": 5})
    col.document("none").set({"other": 1})
    got = [s.id for s in col.where(filter=FieldFilter("createdAt", "<", base + timedelta(days=1))).stream()]
    assert got == ["old"]


def test_array_contains(db):
    col = db.collection("conversations")
    col.document("a").set({"uids": ["u1", "u2"]})
    col.document("b").set({"uids": ["u2"]})
    col.document("c").set({"uids": "u1"})  # not an array
    col.document("d").set({"uids": [1]})
    assert [s.id for s in col.where(filter=FieldFilter("uids", "array_contains", "u1")).stream()] == ["a"]
    assert [s.id for s in col.where("uids", "array_contains", "u2").stream()] == ["a", "b"]
    assert [s.id for s in col.where(filter=FieldFilter("uids", "array_contains", "1")).stream()] == []


def test_equality_is_type_strict_and_none_matches_null(db):
    col = db.collection("c")
    col.document("t").set({"v": True})
    col.document("one").set({"v": 1})
    col.document("s").set({"v": "1"})
    col.document("n").set({"v": None})
    col.document("dot").set({"m": {"k": 1}})
    assert [s.id for s in col.where(filter=FieldFilter("v", "==", 1)).stream()] == ["one"]
    assert [s.id for s in col.where(filter=FieldFilter("v", "==", True)).stream()] == ["t"]
    assert [s.id for s in col.where(filter=FieldFilter("v", "==", None)).stream()] == ["n"]
    assert [s.id for s in col.where(filter=FieldFilter("m.k", "==", 1)).stream()] == ["dot"]


def test_order_by_excludes_missing_and_breaks_ties_by_id(db):
    col = db.collection("c")
    for doc_id, seq in (("b", 1), ("a", 1), ("c", 2), ("d", None)):
        col.document(doc_id).set({} if seq is None else {"seq": seq})
    assert [s.id for s in col.order_by("seq").stream()] == ["a", "b", "c"]
    assert [s.id for s in col.order_by("seq", direction="DESCENDING").stream()] == ["c", "b", "a"]
    assert [s.id for s in col.stream()] == ["a", "b", "c", "d"]


def test_limit_to_last_returns_the_tail_in_query_order(db):
    col = db.collection("messages")
    for i in range(6):
        col.document(f"m{i}").set({"convKey": "k", "seq": i})
    col.document("other").set({"convKey": "x", "seq": 99})
    q = col.where(filter=FieldFilter("convKey", "==", "k")).order_by("seq").limit_to_last(2)
    assert [s.to_dict()["seq"] for s in q.get()] == [4, 5]
    with pytest.raises(ValueError):
        q.stream()


def test_start_after_pages_through_ties(db):
    col = db.collection("alerts")
    for i in range(7):
        col.document(f"a{i}").set({"ts": i // 2})
    seen = []
    last = None
    while True:
        q = col.order_by("ts").limit(3)
        if last is not None:
            q = q.start_after(last)
        page = q.get()
        seen.extend(s.id for s in page)
        if len(page) < 3:
            break
        last = page[-1]
    assert seen == [f"a{i}" for i in range(7)]
    desc = col.order_by("ts", direction="DESCENDING").limit(2).get()
    after = col.order_by("ts", direction="DESCENDING").start_after(desc[-1]).get()
    assert [s.id for s in desc + after] == ["a6", "a5", "a4", "a3", "a2", "a1", "a0"]


def test_collection_group_spans_parents(db):
    db.collection("devices").document("p1").collection("locations").document("x").set({"n": 1})
    db.collection("devices").document("p2").collection("locations").document("y").set({"n": 2})
    db.collection("locations").document("z").set({"n": 3})
    db.collection("devices").document("p1").collection("battery").document("b").set({"n": 4})
    snaps = list(db.collection_group("locations").order_by("n").stream())
    assert [s.reference.path for s in snaps] == [
        "devices/p1/locations/x",
        "devices/p2/locations/y",
        "locations/z",
    ]
    assert snaps[0].reference.parent.parent.id == "p1"
    only_p1 = db.collection("devices").document("p1").collection("locations").stream()
    assert [s.id for s in only_p1] == ["x"]


def test_unsupported_operators_and_attributes_raise_not_implemented(db):
    col = db.collection("c")
    for op in ("in", "not-in", "!=", "array_contains_any"):
        with pytest.raises(NotImplementedError, match="operator"):
            col.where(filter=FieldFilter("a", op, ["x"] if "in" in op or "any" in op else "x")).get()
    with pytest.raises(NotImplementedError, match="CollectionReference.select"):
        col.select(["a"])
    with pytest.raises(NotImplementedError, match="Client.bulk_writer"):
        db.bulk_writer()
    with pytest.raises(NotImplementedError, match="DocumentSnapshot.create_time"):
        col.document("d").get().create_time  # noqa: B018


def test_transaction_commits_once_and_rolls_back_on_error(db):
    ref = db.collection("settings").document("meta")
    ref.set({"seqCounter": 1})

    @transactional
    def bump(txn):
        snap = ref.get(transaction=txn)
        txn.update(ref, {"seqCounter": snap.to_dict()["seqCounter"] + 1})

    bump(db.transaction())
    assert ref.get().to_dict() == {"seqCounter": 2}

    @transactional
    def conflict(txn):
        txn.set(ref, {"seqCounter": 100})
        txn.create(db.collection("aliases").document("taken"), {"uid": "x"})

    db.collection("aliases").document("taken").set({"uid": "first"})
    with pytest.raises(AlreadyExists):
        conflict(db.transaction())
    assert ref.get().to_dict() == {"seqCounter": 2}
    # The writer lock was released: a plain write still goes through.
    ref.update({"seqCounter": 3})


def test_transactional_read_after_write_is_refused(db):
    ref = db.collection("c").document("d")

    @transactional
    def bad(txn):
        txn.set(ref, {"a": 1})
        ref.get(transaction=txn)

    with pytest.raises(ReadAfterWriteError):
        bad(db.transaction())
    assert not ref.get().exists


def test_change_feed_reports_committed_writes_only(db):
    events = []
    unsubscribe = db.subscribe(lambda *e: events.append(e))
    loc = db.collection("devices").document("p1").collection("locations").document("x")
    loc.set({"n": 1})
    loc.update({"n": 2})
    loc.delete()
    assert events == [
        ("devices/p1/locations/x", "locations", "devices/p1/locations", {"n": 1}),
        ("devices/p1/locations/x", "locations", "devices/p1/locations", {"n": 2}),
        ("devices/p1/locations/x", "locations", "devices/p1/locations", None),
    ]

    events.clear()
    ref = db.collection("c").document("d")

    @transactional
    def fails(txn):
        txn.set(ref, {"a": 1})
        raise RuntimeError("abandon")

    with pytest.raises(RuntimeError):
        fails(db.transaction())
    assert events == []

    batch = db.batch()
    batch.set(ref, {"a": 1})
    batch.set(db.collection("c").document("e"), {"b": SERVER_TIMESTAMP})
    batch.commit()
    assert [e[0] for e in events] == ["c/d", "c/e"]
    assert isinstance(events[1][3]["b"], datetime)

    unsubscribe()
    ref.set({"a": 2})
    assert len(events) == 2


def test_change_feed_listener_failure_does_not_fail_the_write(db):
    db.subscribe(lambda *e: 1 / 0)
    ref = db.collection("c").document("d")
    ref.set({"a": 1})
    assert ref.get().to_dict() == {"a": 1}


def test_refuses_a_database_another_process_holds_for_writing(tmp_path):
    path = str(tmp_path / "pager.sqlite")
    sqlitestore.Client(path).close()  # create it, WAL and schema
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlitestore.DatabaseInUse):
            sqlitestore.Client(path)
    finally:
        other.execute("ROLLBACK")
        other.close()
    sqlitestore.Client(path).close()


def test_refuses_a_second_client_on_the_same_file(db):
    with pytest.raises(sqlitestore.DatabaseInUse):
        sqlitestore.Client(db._path)
