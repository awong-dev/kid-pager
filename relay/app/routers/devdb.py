"""Raw document access for the end-to-end suite. `DEV_MODE=1` only: no authentication.

Values use the SQLite shim JSON encoding: `{"$ts": <int microseconds UTC>}`, `{"$b64": "..."}`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import sqlitestore
from app.db.firestore import get_db

router = APIRouter(prefix="/api/dev/db")


class DocWrite(BaseModel):
    path: str
    data: dict[str, Any]


def _segments(path: str, *, document: bool) -> list[str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts or (len(parts) % 2 == 0) != document:
        kind = "document" if document else "collection"
        raise HTTPException(status_code=400, detail=f"not a {kind} path: {path!r}")
    return parts


def _doc_ref(path: str):
    parts = _segments(path, document=True)
    return get_db().collection(*parts[:-1]).document(parts[-1])


def _row(snap) -> dict[str, Any]:
    return {
        "path": snap.reference.path,
        "id": snap.id,
        "data": sqlitestore.to_wire(snap.to_dict() or {}),
    }


@router.get("/doc")
def get_doc(path: str = Query(...)) -> dict[str, Any]:
    snap = _doc_ref(path).get()
    data = sqlitestore.to_wire(snap.to_dict() or {}) if snap.exists else None
    return {"path": path.strip("/"), "exists": snap.exists, "data": data}


@router.put("/doc")
def put_doc(body: DocWrite) -> dict[str, bool]:
    _doc_ref(body.path).set(sqlitestore.from_wire(body.data))
    return {"ok": True}


@router.patch("/doc")
def patch_doc(body: DocWrite) -> dict[str, bool]:
    _doc_ref(body.path).set(sqlitestore.from_wire(body.data), merge=True)
    return {"ok": True}


@router.delete("/doc")
def delete_doc(path: str = Query(...)) -> dict[str, bool]:
    _doc_ref(path).delete()
    return {"ok": True}


@router.get("/list")
def list_docs(parent: str | None = None, group: str | None = None) -> dict[str, Any]:
    if (parent is None) == (group is None):
        raise HTTPException(status_code=400, detail="exactly one of parent or group")
    if parent is not None:
        query = get_db().collection(*_segments(parent, document=False))
    else:
        query = get_db().collection_group(group or "")
    return {"docs": [_row(snap) for snap in query.stream()]}


@router.post("/wipe")
def wipe() -> dict[str, int]:
    db = get_db()
    if isinstance(db, sqlitestore.Client):
        return {"deleted": db.delete_all()}
    return {"deleted": sum(db.recursive_delete(c) for c in db.collections())}
