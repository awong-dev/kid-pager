"""Super-only family freeze, export, backup and import (`.kpfam` files).

The export passphrase travels in the `X-Export-Passphrase` header."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app import familyexport
from app.auth import require_super
from app.config import Settings
from app.db.firestore import get_db
from app.routers.admin import require_admin_write_rate_limit
from app.store import families as families_store
from app.store.families import Family

router = APIRouter(prefix="/api/admin/families", dependencies=[Depends(require_super)])

FamilyId = Annotated[str, Path(min_length=1)]
PassphraseHeader = Annotated[str | None, Header(alias="X-Export-Passphrase")]
DEFAULT_MAX_UPLOAD_MB = 512


@router.post("/{fid}/freeze", dependencies=[Depends(require_admin_write_rate_limit)])
def freeze(fid: FamilyId) -> Family:
    """Ingest, `Routing.send` and bridge events refuse the family's traffic until unfrozen."""
    try:
        return families_store.set_frozen(fid, True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such family") from exc


@router.post("/{fid}/unfreeze", dependencies=[Depends(require_admin_write_rate_limit)])
def unfreeze(fid: FamilyId) -> Family:
    try:
        return families_store.set_frozen(fid, False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such family") from exc


def _pairs(values: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        key, sep, val = v.partition("=")
        if not sep or not key or not val:
            raise HTTPException(
                status_code=422, detail=f"{what} must look like old=new, got {v!r}"
            )
        out[key] = val
    return out


def _stream(
    fid: str, request: Request, passphrase: str | None, history: bool, *, backup: bool
) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    opts = familyexport.ExportOptions(
        history=history,
        backup=backup,
        source_kind=familyexport.source_kind(),
        base_url=settings.public_base_url,
    )
    try:
        lines = familyexport.export_family(
            get_db(), fid, opts, encrypted=bool(passphrase)
        )
    except familyexport.FamilyNotFound as exc:
        raise HTTPException(status_code=404, detail="no such family") from exc
    except familyexport.NotFrozen as exc:
        raise HTTPException(
            status_code=409,
            detail="freeze the family first (or take a backup of a live family)",
        ) from exc
    except familyexport.PassphraseRequired as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = f"{fid}-{datetime.now(UTC).date().isoformat()}.kpfam"
    return StreamingResponse(
        familyexport.pack(lines, passphrase),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/{fid}/export")
def export(
    fid: FamilyId,
    request: Request,
    passphrase: PassphraseHeader = None,
    history: Annotated[bool, Query()] = True,
) -> StreamingResponse:
    if not passphrase:
        raise HTTPException(
            status_code=400, detail="the X-Export-Passphrase header is required"
        )
    return _stream(fid, request, passphrase, history, backup=False)


@router.get("/{fid}/backup")
def backup(
    fid: FamilyId,
    request: Request,
    passphrase: PassphraseHeader = None,
    history: Annotated[bool, Query()] = True,
) -> StreamingResponse:
    return _stream(fid, request, passphrase, history, backup=True)


async def _spool_upload(request: Request) -> tempfile.SpooledTemporaryFile:
    cap = (
        int(os.environ.get("FAMILY_IMPORT_MAX_MB", DEFAULT_MAX_UPLOAD_MB)) * 1024 * 1024
    )
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115 -- closed by caller
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            spool.close()
            raise HTTPException(status_code=413, detail="file too large")
        spool.write(chunk)
    return spool


@router.post("/import", dependencies=[Depends(require_admin_write_rate_limit)])
async def import_family(
    request: Request,
    passphrase: PassphraseHeader = None,
    dry_run: Annotated[bool, Query(alias="dryRun")] = False,
    as_id: Annotated[str | None, Query(alias="as")] = None,
    alias: Annotated[list[str] | None, Query()] = None,
    super_alias: Annotated[str | None, Query(alias="super")] = None,
    email: Annotated[list[str] | None, Query()] = None,
    admin_email: Annotated[list[str] | None, Query(alias="adminEmail")] = None,
):
    opts = familyexport.ImportOptions(
        as_id=as_id,
        alias_map=_pairs(alias or [], "alias"),
        super_alias=super_alias,
        emails={
            **_pairs(admin_email or [], "adminEmail"),
            **_pairs(email or [], "email"),
        },
    )
    spool = await _spool_upload(request)
    try:
        src = familyexport.KpfamFile(spool, passphrase)

        def _run():
            db = get_db()
            if dry_run:
                return familyexport.plan_import(db, src, opts)
            return familyexport.apply_import(db, src, opts)

        try:
            result = await run_in_threadpool(_run)
        except familyexport.BadPassphrase as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except familyexport.CorruptFile as exc:
            raise HTTPException(
                status_code=400, detail=f"not a usable family file: {exc}"
            ) from exc
        except familyexport.ImportRefused as exc:
            raise HTTPException(
                status_code=409, detail=exc.report.model_dump(mode="json")
            ) from exc
    finally:
        spool.close()
    if dry_run:
        return result
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=201, content=result.model_dump(mode="json"))
