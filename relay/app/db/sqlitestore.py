"""The `google.cloud.firestore` client API over SQLite: a subset of the real client.
Unsupported attributes raise `NotImplementedError`; stored `{"$ts": us}` and `{"$b64": ...}`."""

from __future__ import annotations

import base64
import fcntl
import itertools
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.base_collection import _auto_id
from google.cloud.firestore_v1.base_query import BaseCompositeFilter, FieldFilter
from google.cloud.firestore_v1.batch import WriteBatch as _GoogleWriteBatch
from google.cloud.firestore_v1.field_path import FieldPath, parse_field_path
from google.cloud.firestore_v1.transaction import Transaction as _GoogleTransaction
from google.cloud.firestore_v1.types import StructuredQuery

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)
_IS_NULL = StructuredQuery.UnaryFilter.Operator.IS_NULL
_INDEXES_JSON = Path(__file__).resolve().parents[2] / "firestore.indexes.json"
_SIMPLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_txn_ids = itertools.count(1)

Listener = Callable[[str, str, str, "dict[str, Any] | None"], None]


def _unsupported(owner: str, name: str) -> NotImplementedError:
    return NotImplementedError(
        f"sqlitestore: {owner}.{name} is not implemented"
    )


class DatabaseInUse(RuntimeError):
    """Another process holds the database for writing."""


def _micros(dt: datetime) -> int:
    # google's encoder treats a naive datetime as UTC; so does the shim.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - _EPOCH) // _ONE_US


def _from_micros(us: int) -> DatetimeWithNanoseconds:
    d = _EPOCH + timedelta(microseconds=us)
    return DatetimeWithNanoseconds(
        d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, tzinfo=UTC
    )


def _to_json(value: Any) -> Any:
    """Python value (as google's `decode_value` produces it) -> JSON form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return {"$ts": _micros(value)}
    if isinstance(value, bytes):
        return {"$b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    raise _unsupported("value", type(value).__name__)


def _hook(obj: dict[str, Any]) -> Any:
    if len(obj) == 1:
        if "$ts" in obj:
            return _from_micros(obj["$ts"])
        if "$b64" in obj:
            return base64.b64decode(obj["$b64"])
    return obj


def _decode(text: str) -> dict[str, Any]:
    return json.loads(text, object_hook=_hook)


def _dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def to_wire(value: Any) -> Any:
    """A document value -> the JSON form (`$ts` / `$b64`)."""
    return _to_json(value)


def from_wire(value: Any) -> Any:
    """Inverse of `to_wire` for already-parsed JSON."""
    return json.loads(json.dumps(value), object_hook=_hook)


def _same(a: Any, b: Any) -> bool:
    """Firestore value equality: a bool never equals a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _get_path(doc: dict[str, Any], parts: list[str]) -> tuple[bool, Any]:
    cur: Any = doc
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    return True, cur


def _set_path(doc: dict[str, Any], parts: list[str], value: Any) -> None:
    cur = doc
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _del_path(doc: dict[str, Any], parts: list[str]) -> None:
    cur: Any = doc
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _json_path(parts: list[str]) -> str:
    """A SQLite JSON path literal; simple keys stay bare so it matches the index DDL textually."""
    out = "$"
    for p in parts:
        if '"' in p or "'" in p:
            raise _unsupported("field path", repr(p))
        out += f".{p}" if _SIMPLE_KEY.match(p) else f'."{p}"'
    return f"'{out}'"


def _field_parts(field_path: str) -> list[str]:
    if field_path == "__name__":
        raise _unsupported("Query", "__name__ field path")
    return list(FieldPath.from_string(field_path).parts)


def _rank_expr(parts: list[str]) -> str:
    p = _json_path(parts)
    ts = _json_path([*parts, "$ts"])
    b64 = _json_path([*parts, "$b64"])
    return (
        f"CASE json_type(data,{p}) WHEN 'null' THEN 0 WHEN 'true' THEN 1 WHEN 'false' THEN 1 "
        f"WHEN 'integer' THEN 2 WHEN 'real' THEN 2 WHEN 'text' THEN 4 WHEN 'array' THEN 8 "
        f"ELSE CASE WHEN json_type(data,{ts})='integer' THEN 3 "
        f"WHEN json_type(data,{b64})='text' THEN 5 ELSE 9 END END"
    )


def _value_expr(parts: list[str]) -> str:
    p = _json_path(parts)
    ts = _json_path([*parts, "$ts"])
    b64 = _json_path([*parts, "$b64"])
    return (
        f"CASE json_type(data,{p}) WHEN 'object' THEN "
        f"COALESCE(json_extract(data,{ts}),json_extract(data,{b64})) "
        f"ELSE json_extract(data,{p}) END"
    )


def _py_sort_key(value: Any) -> tuple[int, Any]:
    """`(_rank_expr, _value_expr)` for a JSON-form value, for cursors."""
    if value is None:
        return 0, None
    if isinstance(value, bool):
        return 1, int(value)
    if isinstance(value, (int, float)):
        return 2, value
    if isinstance(value, str):
        return 4, value
    if isinstance(value, dict) and len(value) == 1 and "$ts" in value:
        return 3, value["$ts"]
    if isinstance(value, dict) and len(value) == 1 and "$b64" in value:
        return 5, value["$b64"]
    raise _unsupported("Query.start_after", f"cursor on a {type(value).__name__} value")


def _typed_compare(parts: list[str], value: Any) -> tuple[str, str, Any]:
    """`(type guard, expression, bound value)`; matches only values of the filter's type."""
    p = _json_path(parts)
    if isinstance(value, bool):
        return f"json_type(data,{p}) IN ('true','false')", f"json_extract(data,{p})", int(value)
    if isinstance(value, (int, float)):
        return f"json_type(data,{p}) IN ('integer','real')", f"json_extract(data,{p})", value
    if isinstance(value, str):
        return f"json_type(data,{p})='text'", f"json_extract(data,{p})", value
    if isinstance(value, datetime):
        ts = _json_path([*parts, "$ts"])
        return f"json_type(data,{ts})='integer'", f"json_extract(data,{ts})", _micros(value)
    raise _unsupported("Query.where", f"{type(value).__name__} value")


def _element_guard(value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return "e.type IN ('true','false')", int(value)
    if isinstance(value, (int, float)):
        return "e.type IN ('integer','real')", value
    if isinstance(value, str):
        return "e.type='text'", value
    raise _unsupported("Query.where", f"array_contains on a {type(value).__name__} value")


_SQL_OPS = {"==": "=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def _compile_filter(f: FieldFilter) -> tuple[str, list[Any], bool]:
    """`(sql, params, is_inequality)` for one `FieldFilter`."""
    parts = _field_parts(f.field_path)
    op = f.op_string
    if f.value is None:
        if op == _IS_NULL:
            return f"json_type(data,{_json_path(parts)})='null'", [], False
        raise _unsupported("Query.where", f"operator {op!r}")
    if op == "array_contains":
        p = _json_path(parts)
        guard, bound = _element_guard(f.value)
        sql = (
            f"json_type(data,{p})='array' AND EXISTS "
            f"(SELECT 1 FROM json_each(data,{p}) AS e WHERE {guard} AND e.value = ?)"
        )
        return sql, [bound], False
    if op not in _SQL_OPS:
        raise _unsupported("Query.where", f"operator {op!r}")
    guard, expr, bound = _typed_compare(parts, f.value)
    return f"{guard} AND {expr} {_SQL_OPS[op]} ?", [bound], op != "=="


class WriteResult(NamedTuple):
    update_time: DatetimeWithNanoseconds


class DocumentSnapshot:
    __slots__ = ("_text", "reference")

    def __init__(self, reference: DocumentReference, text: str | None) -> None:
        self.reference = reference
        self._text = text

    @property
    def exists(self) -> bool:
        return self._text is not None

    @property
    def id(self) -> str:
        return self.reference.id

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._text is None else _decode(self._text)

    def get(self, field_path: str) -> Any:
        if self._text is None:
            return None
        found, value = _get_path(_decode(self._text), parse_field_path(field_path))
        if not found:
            raise KeyError(f"No field {field_path!r} in document {self.reference.path}")
        return value

    def _raw(self) -> dict[str, Any]:
        return json.loads(self._text or "{}")

    def __getattr__(self, name: str) -> Any:
        raise _unsupported("DocumentSnapshot", name)


class DocumentReference:
    def __init__(self, client: Client, path: tuple[str, ...]) -> None:
        if len(path) % 2 != 0 or not all(path):
            raise ValueError(f"A document path must have an even number of segments: {path}")
        self._client = client
        self._path = path

    @property
    def id(self) -> str:
        return self._path[-1]

    @property
    def path(self) -> str:
        return "/".join(self._path)

    @property
    def parent(self) -> CollectionReference:
        return CollectionReference(self._client, self._path[:-1])

    @property
    def _document_path(self) -> str:
        # What google's batch code keys writes by.
        return f"{self._client._database_string}/documents/{self.path}"

    def collection(self, collection_id: str) -> CollectionReference:
        return CollectionReference(self._client, (*self._path, *collection_id.split("/")))

    def get(self, field_paths: Any = None, transaction: Transaction | None = None) -> DocumentSnapshot:
        if field_paths is not None:
            raise _unsupported("DocumentReference.get", "field_paths")
        conn = self._client._conn_for(transaction)
        row = conn.execute("SELECT data FROM docs WHERE path = ?", (self.path,)).fetchone()
        return DocumentSnapshot(self, row[0] if row else None)

    def create(self, document_data: dict[str, Any]) -> WriteResult:
        batch = self._client.batch()
        batch.create(self, document_data)
        return batch.commit()[0]

    def set(self, document_data: dict[str, Any], merge: bool | list[str] = False) -> WriteResult:
        batch = self._client.batch()
        batch.set(self, document_data, merge=merge)
        return batch.commit()[0]

    def update(self, field_updates: dict[str, Any], option: Any = None) -> WriteResult:
        batch = self._client.batch()
        batch.update(self, field_updates, option=option)
        return batch.commit()[0]

    def delete(self, option: Any = None) -> DatetimeWithNanoseconds:
        batch = self._client.batch()
        batch.delete(self, option=option)
        return batch.commit()[0].update_time

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, DocumentReference)
            and other._client is self._client
            and other._path == self._path
        )

    def __hash__(self) -> int:
        return hash(self._path)

    def __repr__(self) -> str:
        return f"<sqlitestore.DocumentReference {self.path}>"

    def __getattr__(self, name: str) -> Any:
        raise _unsupported("DocumentReference", name)


class Query:
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"

    def __init__(
        self,
        client: Client,
        parent_path: str | None,
        collection_id: str,
        *,
        filters: tuple[FieldFilter, ...] = (),
        orders: tuple[tuple[str, str], ...] = (),
        limit: int | None = None,
        limit_to_last: bool = False,
        start_after: DocumentSnapshot | None = None,
    ) -> None:
        self._client = client
        # None = collection group (every collection named `collection_id`).
        self._parent_path = parent_path
        self._collection_id = collection_id
        self._filters = filters
        self._orders = orders
        self._limit = limit
        self._limit_to_last = limit_to_last
        self._start_after = start_after

    def _copy(self, **changes: Any) -> Query:
        kw: dict[str, Any] = {
            "filters": self._filters,
            "orders": self._orders,
            "limit": self._limit,
            "limit_to_last": self._limit_to_last,
            "start_after": self._start_after,
        }
        kw.update(changes)
        return Query(self._client, self._parent_path, self._collection_id, **kw)

    def where(
        self,
        field_path: str | None = None,
        op_string: str | None = None,
        value: Any = None,
        *,
        filter: Any = None,
    ) -> Query:
        if filter is not None:
            if field_path is not None or op_string is not None:
                raise ValueError(
                    "Can't pass in both the positional arguments and 'filter' at the same time"
                )
            if isinstance(filter, BaseCompositeFilter):
                raise _unsupported("Query.where", type(filter).__name__)
            if not isinstance(filter, FieldFilter):
                raise _unsupported("Query.where", f"filter of type {type(filter).__name__}")
            f = filter
        else:
            f = FieldFilter(field_path, op_string, value)
        return self._copy(filters=(*self._filters, f))

    def order_by(self, field_path: str, direction: str = ASCENDING) -> Query:
        if direction not in (self.ASCENDING, self.DESCENDING):
            raise ValueError(f"Invalid direction {direction!r}")
        return self._copy(orders=(*self._orders, (field_path, direction)))

    def limit(self, count: int) -> Query:
        return self._copy(limit=count, limit_to_last=False)

    def limit_to_last(self, count: int) -> Query:
        return self._copy(limit=count, limit_to_last=True)

    def start_after(self, document_fields_or_snapshot: Any) -> Query:
        if not isinstance(document_fields_or_snapshot, DocumentSnapshot):
            raise _unsupported("Query.start_after", "field values (only a snapshot)")
        return self._copy(start_after=document_fields_or_snapshot)

    def get(self, transaction: Transaction | None = None) -> list[DocumentSnapshot]:
        return self._run(transaction)

    def stream(self, transaction: Transaction | None = None) -> Iterator[DocumentSnapshot]:
        if self._limit_to_last:
            raise ValueError(
                "Query results for queries that include limit_to_last() constraints "
                "cannot be streamed. Use Query.get() instead."
            )
        return iter(self._run(transaction))


    def _sql(self) -> tuple[str, list[Any]]:
        if self._parent_path is None:
            where = ["collection = ?"]
            params: list[Any] = [self._collection_id]
            name_expr = "replace(path,'/',char(1))"
        else:
            where = ["parent = ?"]
            params = [f"{self._parent_path}/{self._collection_id}" if self._parent_path else self._collection_id]
            name_expr = "doc_id"
        inequality_fields: list[str] = []
        for f in self._filters:
            sql, ps, ineq = _compile_filter(f)
            where.append(sql)
            params.extend(ps)
            if ineq and f.field_path not in inequality_fields:
                inequality_fields.append(f.field_path)

        # Firestore orders by the inequality fields first when the query names no order, and
        # always breaks ties by document name in the direction of the last order.
        orders = list(self._orders)
        ordered = {fp for fp, _ in orders}
        last_dir = orders[-1][1] if orders else self.ASCENDING
        for fp in sorted(inequality_fields):
            if fp not in ordered:
                orders.append((fp, last_dir))
        keys: list[tuple[str, bool]] = []
        for fp, direction in orders:
            parts = _field_parts(fp)
            # A document without the ordered field is not in the result.
            where.append(f"json_type(data,{_json_path(parts)}) IS NOT NULL")
            desc = direction == self.DESCENDING
            keys.append((_rank_expr(parts), desc))
            keys.append((_value_expr(parts), desc))
        name_desc = (orders[-1][1] == self.DESCENDING) if orders else False
        keys.append((name_expr, name_desc))

        if self._start_after is not None:
            snap = self._start_after
            raw = snap._raw()
            values: list[Any] = []
            for fp, _ in orders:
                found, v = _get_path(raw, _field_parts(fp))
                if not found:
                    raise ValueError(f"Cursor snapshot is missing the order field {fp!r}")
                values.extend(_py_sort_key(v))
            values.append(snap.id if self._parent_path is not None else snap.reference.path.replace("/", "\x01"))
            ors = []
            for i, (expr, desc) in enumerate(keys):
                terms = [f"({keys[j][0]}) IS ?" for j in range(i)]
                terms.append(f"({expr}) {'<' if desc else '>'} ?")
                ors.append("(" + " AND ".join(terms) + ")")
                params.extend(values[: i + 1])
            where.append("(" + " OR ".join(ors) + ")")

        if self._limit_to_last:
            keys = [(e, not d) for e, d in keys]
        order_sql = ", ".join(f"{e} {'DESC' if d else 'ASC'}" for e, d in keys)
        sql = f"SELECT path, data FROM docs WHERE {' AND '.join(where)} ORDER BY {order_sql}"
        if self._limit is not None:
            sql += " LIMIT ?"
            params.append(self._limit)
        return sql, params

    def _run(self, transaction: Transaction | None) -> list[DocumentSnapshot]:
        sql, params = self._sql()
        conn = self._client._conn_for(transaction)
        rows = conn.execute(sql, params).fetchall()
        if self._limit_to_last:
            rows.reverse()
        client = self._client
        return [DocumentSnapshot(DocumentReference(client, tuple(p.split("/"))), d) for p, d in rows]

    def __getattr__(self, name: str) -> Any:
        raise _unsupported(type(self).__name__, name)


class CollectionReference(Query):
    def __init__(self, client: Client, path: tuple[str, ...]) -> None:
        if len(path) % 2 != 1 or not all(path):
            raise ValueError(f"A collection path must have an odd number of segments: {path}")
        super().__init__(client, "/".join(path[:-1]), path[-1])
        self._path = path

    @property
    def id(self) -> str:
        return self._path[-1]

    @property
    def parent(self) -> DocumentReference | None:
        return DocumentReference(self._client, self._path[:-1]) if len(self._path) > 1 else None

    def document(self, document_id: str | None = None) -> DocumentReference:
        return DocumentReference(self._client, (*self._path, document_id or _auto_id()))

    def add(
        self, document_data: dict[str, Any], document_id: str | None = None
    ) -> tuple[DatetimeWithNanoseconds, DocumentReference]:
        ref = self.document(document_id)
        return ref.create(document_data).update_time, ref


class WriteBatch(_GoogleWriteBatch):
    def commit(self, retry: Any = None, timeout: Any = None) -> list[WriteResult]:
        results = self._client._commit_writes(self._write_pbs)
        self.write_results = results
        self.commit_time = results[0].update_time if results else None
        self._write_pbs = []
        self._document_references = {}
        return results


class Transaction(_GoogleTransaction):
    """google's `Transaction`, duck-compatible with `@transactional`; never raises `Aborted`."""

    def _begin(self, retry_id: bytes | None = None) -> None:
        if self.in_progress:
            raise ValueError(f"Cannot begin transaction: {self._id!r} already in progress")
        client: Client = self._client
        client._wlock.acquire()
        try:
            client._writer.execute("BEGIN IMMEDIATE")
        except BaseException:
            client._wlock.release()
            raise
        self._id = f"sqlite-{next(_txn_ids)}".encode()

    def _rollback(self) -> None:
        if not self.in_progress:
            raise ValueError("Cannot rollback transaction: not in progress")
        client: Client = self._client
        try:
            if client._writer.in_transaction:
                client._writer.execute("ROLLBACK")
        finally:
            self._clean_up()
            client._wlock.release()

    def _commit(self) -> list[WriteResult]:
        if not self.in_progress:
            raise ValueError("Cannot commit transaction: not in progress")
        client: Client = self._client
        # On failure (NotFound, AlreadyExists, ...) the SQLite transaction is
        # left open; google's `_Transactional` calls `_rollback` next.
        results, changes = client._apply_writes(self._write_pbs)
        client._writer.execute("COMMIT")
        self._clean_up()
        client._wlock.release()
        client._publish(changes)
        return results

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise _unsupported("Transaction", "get")

    def get_all(self, *args: Any, **kwargs: Any) -> Any:
        raise _unsupported("Transaction", "get_all")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  path        TEXT PRIMARY KEY,
  collection  TEXT NOT NULL,
  parent      TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  data        TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_parent ON docs(parent, doc_id);
CREATE INDEX IF NOT EXISTS docs_collection ON docs(collection);
"""


def _index_ddl(spec: dict[str, Any]) -> list[str]:
    """One expression index per `firestore.indexes.json` entry; `CONTAINS` fields are left out."""
    out = []
    for ix in spec.get("indexes", []):
        fields = [f["fieldPath"] for f in ix["fields"] if "arrayConfig" not in f]
        scope = "collection" if ix.get("queryScope") == "COLLECTION_GROUP" else "parent"
        name = "ix_" + "_".join([ix["collectionGroup"], *fields])
        cols = ", ".join([scope, *(f"json_extract(data,{_json_path(_field_parts(fp))})" for fp in fields)])
        out.append(f"CREATE INDEX IF NOT EXISTS {name} ON docs({cols})")
    for ov in spec.get("fieldOverrides", []):
        scopes = {i.get("queryScope") for i in ov.get("indexes", [])}
        fp = ov["fieldPath"]
        expr = f"json_extract(data,{_json_path(_field_parts(fp))})"
        if "COLLECTION_GROUP" in scopes:
            out.append(
                f"CREATE INDEX IF NOT EXISTS ixg_{ov['collectionGroup']}_{fp} ON docs(collection, {expr})"
            )
    return out


class Client:
    """The shim's `google.cloud.firestore.Client`. One per process."""

    def __init__(self, path: str, project: str = "home") -> None:
        self._path = path
        self._database_string = f"projects/{project}/databases/(default)"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # A second relay on the same file is a configuration error: hold an exclusive lock and
        # refuse a database whose write lock is held.
        self._lock_file = open(f"{path}.lock", "w")  # noqa: SIM115 -- held until close()
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise DatabaseInUse(f"{path} is open in another relay process") from exc
        self._writer = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
        try:
            self._writer.execute("BEGIN IMMEDIATE")
            self._writer.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            self._writer.close()
            self._lock_file.close()
            raise DatabaseInUse(f"{path} is held for writing by another process") from exc
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")
        self._writer.execute("PRAGMA busy_timeout=5000")
        self._writer.executescript(_SCHEMA)
        for ddl in _index_ddl(json.loads(_INDEXES_JSON.read_text())):
            self._writer.execute(ddl)
        self._wlock = threading.RLock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._last_us = 0


    def collection(self, *collection_path: str) -> CollectionReference:
        parts = tuple(p for seg in collection_path for p in seg.split("/"))
        return CollectionReference(self, parts)

    def collection_group(self, collection_id: str) -> Query:
        if "/" in collection_id:
            raise ValueError(f"Invalid collection_id {collection_id!r}. Collection IDs must not contain '/'.")
        return Query(self, None, collection_id)

    def batch(self) -> WriteBatch:
        return WriteBatch(self)

    def transaction(self, **kwargs: Any) -> Transaction:
        return Transaction(self, **kwargs)

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        """Change feed: `fn(path, collection, parent, data_or_None)` after every commit."""
        self._listeners.append(fn)

        def _unsubscribe() -> None:
            if fn in self._listeners:
                self._listeners.remove(fn)

        return _unsubscribe

    def delete_all(self) -> int:
        """Deletes every document (DEV_MODE wipe); not announced on the change feed."""
        with self._wlock:
            cur = self._writer.execute("DELETE FROM docs")
        return cur.rowcount

    def document_count(self) -> int:
        """How many documents the database holds."""
        return self._reader().execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def close(self) -> None:
        with self._readers_lock:
            for conn in self._readers:
                conn.close()
            self._readers.clear()
        self._writer.close()
        self._lock_file.close()

    def __getattr__(self, name: str) -> Any:
        raise _unsupported("Client", name)


    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA query_only=1")
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    def _conn_for(self, transaction: Transaction | None) -> sqlite3.Connection:
        if transaction is None:
            return self._reader()
        # google's own check: in progress, and no read after a staged write.
        _helpers.get_transaction_id(transaction)
        if transaction._client is not self:
            raise ValueError("transaction belongs to another client")
        return self._writer


    def _commit_writes(self, write_pbs: list[Any]) -> list[WriteResult]:
        with self._wlock:
            if self._writer.in_transaction:
                # This thread holds a transaction (RLock re-entry); a direct write would
                # silently join it instead of committing alone.
                raise RuntimeError(
                    "sqlitestore: non-transactional write inside a transaction on the same thread"
                )
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                results, changes = self._apply_writes(write_pbs)
                self._writer.execute("COMMIT")
            except BaseException:
                self._writer.execute("ROLLBACK")
                raise
        self._publish(changes)
        return results

    def _commit_time(self) -> int:
        # One timestamp per commit, as Firestore's commit time; strictly
        # increasing so SERVER_TIMESTAMP never ties between two commits.
        now = _micros(datetime.now(UTC))
        self._last_us = max(now, self._last_us + 1)
        return self._last_us

    def _apply_writes(
        self, write_pbs: list[Any]
    ) -> tuple[list[WriteResult], list[tuple[str, str | None]]]:
        now_us = self._commit_time()
        update_time = _from_micros(now_us)
        prefix = f"{self._database_string}/documents/"
        changes: list[tuple[str, str | None]] = []
        conn = self._writer
        for w in write_pbs:
            pb = w._pb
            op = pb.WhichOneof("operation")
            if op == "update":
                name = pb.update.name
            elif op == "delete":
                name = pb.delete
            else:
                raise _unsupported("Write", str(op))
            path = name[len(prefix) :]
            row = conn.execute("SELECT data FROM docs WHERE path = ?", (path,)).fetchone()
            current = json.loads(row[0]) if row else None
            if pb.HasField("current_document"):
                cond = pb.current_document.WhichOneof("condition_type")
                if cond != "exists":
                    raise _unsupported("Precondition", str(cond))
                if pb.current_document.exists and current is None:
                    raise NotFound(f"No document to update: {name}")
                if not pb.current_document.exists and current is not None:
                    raise AlreadyExists(f"Document already exists: {name}")
            if op == "delete":
                conn.execute("DELETE FROM docs WHERE path = ?", (path,))
                changes.append((path, None))
                continue
            fields = _to_json(_helpers.decode_dict(pb.update.fields, self))
            if pb.HasField("update_mask"):
                doc = current if current is not None else {}
                for fp in pb.update_mask.field_paths:
                    # API form, backtick-quoted where a segment is not a simple name.
                    parts = parse_field_path(fp)
                    found, value = _get_path(fields, parts)
                    if found:
                        _set_path(doc, parts, value)
                    else:
                        _del_path(doc, parts)
            else:
                doc = fields
            for ft in pb.update_transforms:
                self._apply_transform(doc, ft, now_us)
            text = _dumps(doc)
            segs = path.split("/")
            conn.execute(
                "INSERT INTO docs(path, collection, parent, doc_id, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (path, segs[-2], "/".join(segs[:-1]), segs[-1], text, now_us),
            )
            changes.append((path, text))
        return [WriteResult(update_time) for _ in write_pbs], changes

    def _apply_transform(self, doc: dict[str, Any], ft: Any, now_us: int) -> None:
        parts = parse_field_path(ft.field_path)
        kind = ft.WhichOneof("transform_type")
        found, cur = _get_path(doc, parts)
        if kind == "set_to_server_value":
            value: Any = {"$ts": now_us}
        elif kind == "increment":
            inc = _helpers.decode_value(ft.increment, self)
            if found and isinstance(cur, (int, float)) and not isinstance(cur, bool):
                value = cur + inc
            else:
                value = inc
        elif kind == "append_missing_elements":
            value = list(cur) if found and isinstance(cur, list) else []
            for el in (_to_json(_helpers.decode_value(v, self)) for v in ft.append_missing_elements.values):
                if not any(_same(el, x) for x in value):
                    value.append(el)
        elif kind == "remove_all_from_array":
            drop = [_to_json(_helpers.decode_value(v, self)) for v in ft.remove_all_from_array.values]
            base = cur if found and isinstance(cur, list) else []
            value = [x for x in base if not any(_same(x, d) for d in drop)]
        else:
            raise _unsupported("FieldTransform", str(kind))
        _set_path(doc, parts, value)

    def _publish(self, changes: list[tuple[str, str | None]]) -> None:
        listeners = list(self._listeners)
        if not listeners:
            return
        for path, text in changes:
            segs = path.split("/")
            data = None if text is None else _decode(text)
            for fn in listeners:
                try:
                    fn(path, segs[-2], "/".join(segs[:-1]), data)
                except Exception:
                    # The write is already committed; a listener's failure must not surface as
                    # a failed write.
                    logger.exception("sqlitestore: change-feed listener failed for %s", path)


def open_client(path: str, project: str | None = None) -> Client:
    return Client(path, project or os.environ.get("GOOGLE_CLOUD_PROJECT") or "home")
