"""`GET /ca/{sha256hex}.pem`: public, content-addressed CA download (immutable cache headers).

Unauthenticated on purpose: the device trusts the CA by its hash, received over a signed channel.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Response

from app.store import cas as cas_store

router = APIRouter()

# Plain SHA-256 hex digest; anything else cannot be a hash this relay issued.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@router.get("/ca/{sha256hex}.pem")
def get_ca_pem(sha256hex: str) -> Response:
    if not _SHA256_HEX_RE.match(sha256hex):
        raise HTTPException(status_code=404, detail="not found")
    pem = cas_store.get_pem(sha256hex)
    if pem is None:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=pem,
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
