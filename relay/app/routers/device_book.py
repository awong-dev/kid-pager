"""`GET /api/device/book`: device-authenticated book pull (PROTOCOL.md §3.7, §14.7)."""

from __future__ import annotations

import base64
import hmac
import logging
import re

from fastapi import APIRouter, HTTPException, Request, Response

from app import devauth, devcfg
from app.ingest import record_bad_sig
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.wire import DEVICE_ID_RE, N_MAX_EXCLUSIVE

logger = logging.getLogger("relay.device_book")

router = APIRouter(prefix="/api/device")

# §14.7: `X-N` has no leading zeros; `bv` (§3.7, 0..2**32-1) may.
_N_RE = re.compile(r"^(0|[1-9]\d*)$")
_BV_RE = re.compile(r"^\d+$")
# §14.3/§14.7: 8 raw tag bytes in base64url without padding are exactly 11 characters.
_SIG_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_BV_MAX_EXCLUSIVE = 2**32

_BOOK_TOPIC = "/api/device/book"


@router.get("/book")
def get_book(request: Request, bv: str | None = None) -> Response:
    device_id = request.headers.get("X-Device-Id", "")
    n_raw = request.headers.get("X-N", "")
    sig_raw = request.headers.get("X-Sig", "")

    # 1. malformed header or `bv` out of range -> 400
    if (
        not DEVICE_ID_RE.match(device_id)
        or bv is None
        or not _BV_RE.match(bv)
        or not (0 <= int(bv) < _BV_MAX_EXCLUSIVE)
        or not _N_RE.match(n_raw)
        or not (0 <= int(n_raw) < N_MAX_EXCLUSIVE)
        or not _SIG_RE.match(sig_raw)
    ):
        raise HTTPException(status_code=400, detail="malformed request")

    bv_int = int(bv)
    n = int(n_raw)

    # 2. device missing or revoked, `authMode` != `hmac`, or no `deviceSecrets` row -> 404
    device = devices_store.get_device(device_id)
    if device is None or device.revokedAt is not None or device.authMode != "hmac":
        raise HTTPException(status_code=404, detail="not found")
    secret = device_secrets_store.get(device_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="not found")

    # 3. tag verified in constant time; failure -> 401, counted in `sigFailures`
    expected = devauth.request_tag(secret.hmacKey, device_id, n, bv_int)
    got = base64.urlsafe_b64decode(sig_raw + "=" * (-len(sig_raw) % 4))
    if not hmac.compare_digest(got, expected):
        logger.warning(
            "SECURITY bad-sig device=%s endpoint=%s n=%s bv=%s", device_id, _BOOK_TOPIC, n, bv_int
        )
        record_bad_sig(device_id)
        raise HTTPException(status_code=401, detail="bad signature")

    # 4. transaction requiring `n > upN`; otherwise 409, nothing written
    if not device_secrets_store.accept_request_n(device_id, n):
        raise HTTPException(status_code=409, detail="replay")

    # 5. 200 with the §3.7 body; a failure here is a 503 because step 4 already spent `n`
    try:
        body = devcfg.build_book_body(device_id)
    except Exception:
        logger.exception("device book store error device=%s n=%s (n already consumed)", device_id, n)
        raise HTTPException(status_code=503, detail="temporarily unavailable") from None

    # `n` echoes `X-N`, placed last so it precedes `sig` (§14.3)
    body["n"] = n
    payload = devauth.sign_cbor(secret.hmacKey, _BOOK_TOPIC, body)

    logger.info(
        "device book device=%s requested_bv=%s served_bv=%s contacts=%s bytes=%s",
        device_id,
        bv_int,
        body["bv"],
        len(body["c"]),
        len(payload),
    )

    return Response(
        content=payload,
        media_type="application/cbor",
        headers={"Cache-Control": "private, no-store"},
    )
