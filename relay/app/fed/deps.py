"""FastAPI pieces shared by the federation routers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.fed import keys, sign
from app.store import fed_families, fed_links, fed_state
from app.store import rate_limits as rate_limits_store

logger = logging.getLogger(__name__)

MAX_BODY = 256 * 1024
RATE_LIMIT = 600
RATE_WINDOW_S = 60

# One answer for every "not for you", so a caller cannot tell which check failed.
NOT_FOR_YOU = "forbidden"


def not_for_you() -> HTTPException:
    return HTTPException(status_code=403, detail=NOT_FOR_YOU)


def require_server_switch(request: Request) -> None:
    """For the unsigned `info` route: with the server switch off the surface does not exist."""
    if not fed_state.server_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


@dataclass(frozen=True, slots=True)
class SignedRequest:
    family: fed_families.FedFamily
    link: fed_links.FedLink
    body: bytes
    path: str
    used_prev_key: bool = False


def request_target(request: Request) -> str:
    """The path as signed: URL path plus `?query` when there is one."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


async def read_body(request: Request) -> bytes:
    """The body, refusing more than 256 KiB (413) without reading past it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status_code=413, detail="body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def resolve_link(
    to_fed_id: str, from_fed_id: str
) -> tuple[fed_families.FedFamily, fed_links.FedLink]:
    """The local family named by `to` and its link to `from`, both live (server and family
    switches on, link active); otherwise the one indistinguishable 403."""
    if not fed_state.server_enabled():
        logger.info("fed auth failed: server switch off")
        raise not_for_you()
    family = fed_families.get_by_fed_id(to_fed_id)
    if family is None or not fed_state.family_enabled(family.familyId):
        logger.info("fed auth failed: unknown family or switch off to=%s", to_fed_id)
        raise not_for_you()
    link = fed_links.get_for(family.familyId, from_fed_id)
    if link is None or link.state != "active":
        logger.info(
            "fed auth failed: link %s family=%s",
            "missing" if link is None else link.state,
            family.familyId,
        )
        raise not_for_you()
    return family, link


def _authenticate(
    request_headers, method: str, path: str, body: bytes, *, accept_prev_key: bool
) -> SignedRequest:
    try:
        parsed = sign.parse_headers(request_headers)
    except sign.SignatureError:
        logger.info("fed auth failed: missing or malformed signature headers")
        raise not_for_you() from None
    family, link = resolve_link(parsed.receiver, parsed.sender)
    used_prev = False
    try:
        sign.verify(keys.public_from_b64(link.remote.publicKey), parsed, method, path, body)
    except sign.SignatureError as exc:
        if exc.code == "skew":
            logger.info("fed auth failed: timestamp out of range link=%s", link.id)
            raise not_for_you() from None
        prev = link.remote.prevPublicKey
        if not (accept_prev_key and prev):
            logger.info("fed auth failed: bad signature link=%s", link.id)
            raise not_for_you() from None
        try:
            sign.verify(keys.public_from_b64(prev), parsed, method, path, body)
        except sign.SignatureError:
            logger.info("fed auth failed: bad signature (prev key too) link=%s", link.id)
            raise not_for_you() from None
        used_prev = True
    if not rate_limits_store.check_and_increment(
        f"fed:{link.id}", limit=RATE_LIMIT, window_s=RATE_WINDOW_S, now=time.time()
    ):
        raise HTTPException(status_code=429, detail="rate limit")
    return SignedRequest(
        family=family, link=link, body=body, path=path, used_prev_key=used_prev
    )


async def signed_request(request: Request) -> SignedRequest:
    """Reads the body and runs the checks, in order: size (413), headers, family and link
    live, timestamp, signature (every failure of these is the one 403), rate limit (429)."""
    body = await read_body(request)
    path = request_target(request)
    return await run_in_threadpool(
        _authenticate, request.headers, request.method, path, body, accept_prev_key=False
    )


async def signed_moved_request(request: Request) -> SignedRequest:
    """`signed_request` for `POST /fed/v1/moved`, which also accepts the key the link held before
    its last `moved` (the retry of an announcement whose acknowledgement was lost). The route
    only lets that signature through for an identical, already applied announcement."""
    body = await read_body(request)
    path = request_target(request)
    return await run_in_threadpool(
        _authenticate, request.headers, request.method, path, body, accept_prev_key=True
    )
