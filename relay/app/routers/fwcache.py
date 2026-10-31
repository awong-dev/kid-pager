"""`GET /fw-cache/{target16}/from-{base16}.dz`: locally cached OTA deltas (`FW_CACHE=local` only).

Public; the device checks the object against the signed job's `osha`."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import firmware

router = APIRouter()

_HEX16 = re.compile(r"[0-9a-f]{16}")
_NAME = re.compile(r"from-([0-9a-f]{16})\.dz")


@router.get("/fw-cache/{target16}/{name}")
def get_cached_delta(target16: str, name: str, request: Request) -> FileResponse:
    m = _NAME.fullmatch(name)
    if not _HEX16.fullmatch(target16) or m is None:
        raise HTTPException(status_code=404, detail="not found")
    path = firmware.local_file(request.app.state.settings, firmware.cache_path(target16, m.group(1)))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
    )
