"""`/api/admin/move/*` (super only except the import), `GET /api/me/move` and
`GET /api/public/move-info` (docs/MOVE_DESIGN.md §7.4). The bundle passphrase is the
`X-Export-Passphrase` header."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict

from app import deployexport, familyexport, move, move_source, move_target
from app.auth import (
    AuthedUser,
    bearer_scheme,
    principal_for,
    require_super,
    require_user,
)
from app.config import Settings, auth_backend
from app.db.firestore import get_db
from app.routers.admin import get_emqx, require_admin_write_rate_limit
from app.routers.webhooks import _client_ip
from app.store import rate_limits as rate_limits_store

router = APIRouter(prefix="/api/admin/move", dependencies=[Depends(require_super)])
import_router = APIRouter(prefix="/api/admin/move")
me_router = APIRouter(prefix="/api/me")
public_router = APIRouter(prefix="/api/public")

DEFAULT_MAX_UPLOAD_MB = 512
IMPORT_IP_LIMIT = 10
IMPORT_IP_WINDOW_S = 600

PassphraseHeader = Annotated[str | None, Header(alias="X-Export-Passphrase")]


@router.get("")
def get_move(request: Request) -> dict:
    """This relay's move, from whichever side it is on; `state: "none"` when there is none."""
    rec = move.get_record()
    settings: Settings = request.app.state.settings
    if rec is None:
        return {"role": "idle", "state": "none"}
    if rec.role == "source":
        return move_source.status(rec)
    return move_target.status(rec, settings)


@router.post("/preflight", dependencies=[Depends(require_admin_write_rate_limit)])
def preflight(req: move_source.PreflightRequest) -> dict:
    return move_source.preflight(req.targetBaseUrl, req.caPem)


@router.post("/freeze", dependencies=[Depends(require_admin_write_rate_limit)])
def freeze(req: move_source.FreezeRequest) -> dict:
    return {"moveId": move_source.freeze(req)}


@router.get("/bundle")
def bundle(
    request: Request,
    passphrase: PassphraseHeader = None,
    history: Annotated[bool, Query()] = True,
) -> StreamingResponse:
    if not passphrase:
        raise HTTPException(status_code=400, detail="the X-Export-Passphrase header is required")
    settings: Settings = request.app.state.settings
    opts = move_source.export_options(settings, history=history)
    try:
        lines = deployexport.export_deployment(get_db(), opts, encrypted=True)
    except familyexport.NotFrozen as exc:
        raise HTTPException(status_code=409, detail=f"family {exc} is not frozen") from exc
    host = urlsplit(opts.target_base_url).hostname or "bundle"
    name = f"{host}-{datetime.now(UTC).date().isoformat()}.kpdep"
    return StreamingResponse(
        familyexport.pack(lines, passphrase),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/cutover", dependencies=[Depends(require_admin_write_rate_limit)])
def cutover(request: Request, req: move_source.CutoverRequest | None = None) -> dict:
    ids = req.deviceIds if req is not None else None
    return {"ordered": move_source.start_cutover(request.app.state.broker, ids)}


@router.post("/devices/{device_id}/resend", dependencies=[Depends(require_admin_write_rate_limit)])
def resend(request: Request, device_id: str) -> dict:
    move_source.resend(request.app.state.broker, device_id)
    return {"ordered": 1}


@router.post("/devices/{device_id}/cancel", dependencies=[Depends(require_admin_write_rate_limit)])
def cancel_device(request: Request, device_id: str) -> dict:
    move_source.cancel_device(request.app.state.broker, device_id)
    return {"cancelled": 1}


@router.post("/cancel", dependencies=[Depends(require_admin_write_rate_limit)])
def cancel(request: Request) -> dict:
    return {"cancelled": move_source.cancel_move(request.app.state.broker)}


@me_router.get("/move")
def my_move(authed: Annotated[AuthedUser, Depends(require_user)]) -> dict:
    rec = move.get_record()
    banner = move_source.banner_for(authed.uid, rec) if rec is not None else None
    if banner is None:
        raise HTTPException(status_code=404, detail="no move")
    return banner


# ---- the target: Beam, bridges, End ------------------------------------------------------------


class BeamSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    byHand: bool = False


@router.post("/beam/switch", dependencies=[Depends(require_admin_write_rate_limit)])
def beam_switch(request: Request, req: BeamSwitchRequest | None = None) -> dict:
    return move_target.beam_switch(request.app.state.settings, by_hand=bool(req and req.byHand))


@router.post("/beam/switch-back", dependencies=[Depends(require_admin_write_rate_limit)])
def beam_switch_back(request: Request) -> dict:
    return move_target.beam_switch_back(request.app.state.settings)


@router.post("/bridges/send-back", dependencies=[Depends(require_admin_write_rate_limit)])
def send_bridges_back() -> dict:
    return move_target.send_bridges_back()


@router.post("/end", dependencies=[Depends(require_admin_write_rate_limit)])
def end() -> dict:
    return move_target.end_move()


# ---- the target: import ------------------------------------------------------------------------


def import_authority(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> None:
    """A super, or, under home sign-in, anybody while this server has no user and no family (the
    first-run page is open on the same terms) or an interrupted import of its own, which only the
    same bundle can finish. A Firebase server has no such open page, so an empty one imports
    through `python -m app.deployexport import`. The passphrase is chosen by whoever wrote the
    bundle and authenticates nobody."""
    if auth_backend() == "home" and (move_target.unclaimed() or move_target.import_unfinished()):
        if not rate_limits_store.check_and_increment(
            f"move-import:{_client_ip(request)}",
            limit=IMPORT_IP_LIMIT,
            window_s=IMPORT_IP_WINDOW_S,
        ):
            raise HTTPException(status_code=429, detail="too many requests; try again later")
        return
    authed = require_user(request, creds)
    if principal_for(authed).role != "super":
        raise HTTPException(status_code=403, detail="super admin only")


async def _spool_upload(request: Request) -> tempfile.SpooledTemporaryFile:
    cap = int(os.environ.get("FAMILY_IMPORT_MAX_MB", DEFAULT_MAX_UPLOAD_MB)) * 1024 * 1024
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115 -- closed by caller
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            spool.close()
            raise HTTPException(status_code=413, detail="file too large")
        spool.write(chunk)
    return spool


def _pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        key, sep, val = v.partition("=")
        if not sep or not key or not val:
            raise HTTPException(status_code=422, detail=f"email must look like alias=email, got {v!r}")
        out[key] = val
    return out


@import_router.post("/import", dependencies=[Depends(import_authority)])
async def import_bundle(
    request: Request,
    passphrase: PassphraseHeader = None,
    dry_run: Annotated[bool, Query(alias="dryRun")] = False,
    email: Annotated[list[str] | None, Query()] = None,
) -> Any:
    """Dry run or real import of a `.kpdep` body into an empty server (§5.4). The answer is the
    report; a real import adds what it created. A refused import is a `409` with the report."""
    if move.get_record() is not None and not move_target.import_unfinished():
        raise HTTPException(status_code=409, detail="this server already has a move on record")
    spool = await _spool_upload(request)
    try:
        src = deployexport.KpdepFile(spool, passphrase)
        emqx = get_emqx(request)

        def _run() -> Any:
            db = get_db()
            ph = move_target.find_placeholder(db)
            opts = deployexport.DeploymentImportOptions(
                emails=_pairs(email or []),
                public_base_url=request.app.state.settings.public_base_url,
                placeholder_family_ids=[ph.family_id] if ph else [],
                placeholder_uids=[ph.uid] if ph else [],
                placeholder_aliases=[ph.alias] if ph else [],
            )
            if dry_run:
                return deployexport.plan_deployment_import(db, src, opts)
            return deployexport.apply_deployment_import(db, src, opts, broker_admin=emqx)

        try:
            result = await run_in_threadpool(_run)
        except familyexport.BadPassphrase as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except familyexport.CorruptFile as exc:
            raise HTTPException(status_code=400, detail=f"not a usable bundle: {exc}") from exc
        except deployexport.DeploymentImportRefused as exc:
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), **exc.report.model_dump(mode="json")},
            )
        except deployexport.BrokerLoginFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        spool.close()
    if dry_run:
        return result
    return JSONResponse(
        status_code=201,
        content={
            **result.report.model_dump(mode="json"),
            **result.model_dump(mode="json", exclude={"report", "devices", "bridges"}),
        },
    )


# ---- public ------------------------------------------------------------------------------------


@public_router.get("/move-info")
def move_info_route(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return move_target.info(request.app.state.settings)
