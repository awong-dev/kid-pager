"""`/api/live`: server-sent live reads for the home server (`DB_BACKEND=sqlite`); a snapshot per
subscription, then change-feed deltas filtered by `readauth`."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from app import readauth
from app.auth import AuthedUser, Principal, principal_for, require_user
from app.db.firestore import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live", tags=["live"])

HEARTBEAT_S = 25.0
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _valid_path(path: Any, *, doc: bool) -> bool:
    if not isinstance(path, str):
        return False
    segs = path.split("/")
    return all(segs) and (len(segs) % 2 == 0) == doc


def validate_query(q: Any) -> dict[str, Any]:
    """The web's `LiveQuery` (`web/lib/live/types.ts`), or 400."""
    bad = HTTPException(status_code=400, detail="invalid live query")
    if not isinstance(q, dict):
        raise bad
    if "doc" in q:
        if set(q) != {"doc"} or not _valid_path(q["doc"], doc=True):
            raise bad
        return q
    if not set(q) <= {"collection", "group", "where", "orderBy", "limit"}:
        raise bad
    if q.get("group"):
        if (
            not isinstance(q.get("collection"), str)
            or "/" in q["collection"]
            or not q["collection"]
        ):
            raise bad
    elif not _valid_path(q.get("collection"), doc=False):
        raise bad
    for clause in q.get("where") or []:
        if not (
            isinstance(clause, list)
            and len(clause) == 3
            and isinstance(clause[0], str)
            and clause[1] in ("==", "array-contains")
        ):
            raise bad
    for clause in q.get("orderBy") or []:
        if not (
            isinstance(clause, list)
            and len(clause) == 2
            and isinstance(clause[0], str)
            and clause[1] in ("asc", "desc")
        ):
            raise bad
    limit = q.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise bad
    return q


def run_query(q: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The query through the store, as `(path, data)` in the query's order."""
    db = get_db()
    if "doc" in q:
        parent, _, doc_id = q["doc"].rpartition("/")
        snap = db.collection(parent).document(doc_id).get()
        return [(q["doc"], snap.to_dict())] if snap.exists else []
    ref = (
        db.collection_group(q["collection"])
        if q.get("group")
        else db.collection(q["collection"])
    )
    for fld, op, value in q.get("where") or []:
        ref = ref.where(
            filter=FieldFilter(
                fld, "array_contains" if op == "array-contains" else "==", value
            )
        )
    for fld, direction in q.get("orderBy") or []:
        ref = ref.order_by(
            fld, direction="DESCENDING" if direction == "desc" else "ASCENDING"
        )
    if q.get("limit") is not None:
        ref = ref.limit(q["limit"])
    return [(s.reference.path, s.to_dict()) for s in ref.stream()]


def _field(data: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _same(a: Any, b: Any) -> bool:
    # Firestore equality: a boolean never equals a number.
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def matches(q: dict[str, Any], data: dict[str, Any]) -> bool:
    """The query's `where` clauses against one document, plus Firestore's
    rule that `orderBy` excludes a document missing the ordered field."""
    for fld, op, value in q.get("where") or []:
        found, cur = _field(data, fld)
        if not found:
            return False
        if op == "==":
            if not _same(cur, value):
                return False
        elif not (isinstance(cur, list) and any(_same(x, value) for x in cur)):
            return False
    return all(_field(data, fld)[0] for fld, _ in q.get("orderBy") or [])


def targets(q: dict[str, Any], path: str, collection: str, parent: str) -> bool:
    """Whether a change-feed event is in this query's scope at all."""
    if "doc" in q:
        return path == q["doc"]
    if q.get("group"):
        return collection == q["collection"]
    return parent == q["collection"]


def under(q: dict[str, Any], doc_path: str) -> bool:
    """Whether the query reads below the document `doc_path` (a collection
    or document path under `doc_path/`)."""
    if q.get("group"):
        return False
    return (q.get("doc") or q["collection"]).startswith(doc_path + "/")


def _json_default(value: Any) -> Any:
    # The shim's own encoding (§3.2), which `sseProvider.ts` decodes.
    if isinstance(value, datetime):
        return {"$ts": (value - _EPOCH) // timedelta(microseconds=1)}
    if isinstance(value, bytes):
        return {"$b64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _event(payload: dict[str, Any]) -> str:
    return (
        f"data: {json.dumps(payload, default=_json_default, separators=(',', ':'))}\n\n"
    )


def _doc(path: str, data: dict[str, Any] | None) -> dict[str, Any]:
    return {"id": path.rsplit("/", 1)[-1], "path": path, "data": data}


@dataclass
class _Sub:
    query: dict[str, Any]
    paths: set[str] = field(default_factory=set)


@dataclass
class _Stream:
    id: str
    principal: Principal
    loop: asyncio.AbstractEventLoop
    out: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    subs: dict[str, _Sub] = field(default_factory=dict)

    def send(self, payload: dict[str, Any]) -> None:
        self.out.put_nowait(_event(payload))

    def feed(
        self, path: str, collection: str, parent: str, data: dict[str, Any] | None
    ) -> None:
        """Change-feed listener; runs on whichever thread wrote."""
        try:
            self.loop.call_soon_threadsafe(
                self.on_change, path, collection, parent, data
            )
        except RuntimeError:
            pass  # loop closed: the stream is already gone

    def resnapshot(self, sub_id: str, sub: _Sub) -> None:
        """Re-runs the query and sends a fresh `snapshot`, which the client
        takes in place of everything it holds for `sub_id`."""
        rows = [
            (p, d)
            for p, d in run_query(sub.query)
            if readauth.can_read(self.principal, p, d)
        ]
        sub.paths = {p for p, _ in rows}
        self.send(
            {"sub": sub_id, "type": "snapshot", "docs": [_doc(p, d) for p, d in rows]}
        )

    def on_change(
        self, path: str, collection: str, parent: str, data: dict[str, Any] | None
    ) -> None:
        for sub_id, sub in list(self.subs.items()):
            if parent == "devices" and under(sub.query, path):
                # `locations` and `smsLog` are readable through their parent device's fields
                # (readauth): a change to the device can admit or refuse every document below it.
                self.resnapshot(sub_id, sub)
                continue
            if not targets(sub.query, path, collection, parent):
                continue
            hit = (
                data is not None
                and matches(sub.query, data)
                and readauth.can_read(self.principal, path, data)
            )
            if hit:
                kind = "modified" if path in sub.paths else "added"
                sub.paths.add(path)
                self.send({"sub": sub_id, "type": kind, **_doc(path, data)})
            elif path in sub.paths:
                limit = sub.query.get("limit")
                if limit is not None and len(sub.paths) >= limit:
                    # A full window has room again: document N+1, if there
                    # is one, belongs in it.
                    self.resnapshot(sub_id, sub)
                    continue
                sub.paths.discard(path)
                self.send({"sub": sub_id, "type": "removed", **_doc(path, None)})


_streams: dict[str, _Stream] = {}


def _stream_for(stream_id: str, authed: AuthedUser) -> _Stream:
    st = _streams.get(stream_id)
    if st is None or st.principal.uid != authed.uid:
        raise HTTPException(status_code=404, detail="no such live stream")
    return st


@router.get("")
async def open_stream(
    stream: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> StreamingResponse:
    if not _ID_RE.match(stream):
        raise HTTPException(status_code=400, detail="invalid stream id")
    old = _streams.get(stream)
    if old is not None and old.principal.uid != authed.uid:
        raise HTTPException(status_code=409, detail="stream id in use")
    st = _Stream(
        id=stream, principal=principal_for(authed), loop=asyncio.get_running_loop()
    )
    if old is not None:
        old.out.put_nowait(None)  # a reconnect raced its own old connection
    _streams[stream] = st
    unsubscribe = get_db().subscribe(st.feed)

    async def body() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(st.out.get(), HEARTBEAT_S)
                except TimeoutError:
                    yield ": hb\n\n"
                    continue
                if item is None:
                    return
                yield item
        finally:
            unsubscribe()
            if _streams.get(stream) is st:
                del _streams[stream]

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SubscribeBody(BaseModel):
    sub: str
    query: dict[str, Any]


@router.post("/{stream_id}/subscribe")
async def subscribe(
    stream_id: str,
    body: SubscribeBody,
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> dict[str, bool]:
    st = _stream_for(stream_id, authed)
    if not _ID_RE.match(body.sub):
        raise HTTPException(status_code=400, detail="invalid subscription id")
    q = validate_query(body.query)
    if not readauth.query_admissible(st.principal, q):
        raise HTTPException(status_code=403, detail="query not permitted")
    # One synchronous step: no await between the snapshot read and the subscription's registration.
    rows = [(p, d) for p, d in run_query(q) if readauth.can_read(st.principal, p, d)]
    st.subs[body.sub] = _Sub(query=q, paths={p for p, _ in rows})
    st.send(
        {"sub": body.sub, "type": "snapshot", "docs": [_doc(p, d) for p, d in rows]}
    )
    return {"ok": True}


@router.delete("/{stream_id}/subscribe/{sub}", status_code=204)
async def unsubscribe(
    stream_id: str,
    sub: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> Response:
    st = _stream_for(stream_id, authed)
    st.subs.pop(sub, None)
    return Response(status_code=204)
