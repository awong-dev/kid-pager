"""`POST /webhooks/mqtt` (broker rule engine) and `POST /webhooks/gchat`.

PROTOCOL.md §3.4: malformed device payloads get a 200 and are dropped; auth failures do not.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from google.api_core.exceptions import GoogleAPICallError
from starlette.concurrency import run_in_threadpool

from app.backends import gchat as gchat_backend
from app.backends.resolve import USAGE_HINT, resolve_reply
from app.broker import BrokerClient
from app.ingest import Ingest
from app.routing import Routing
from app.store import backends as backends_store
from app.store import rate_limits as rate_limits_store

logger = logging.getLogger("relay.webhooks")

router = APIRouter()

DEFAULT_WEBHOOK_IP_LIMIT = 30
DEFAULT_WEBHOOK_IP_WINDOW_S = 60


def _webhook_ip_rate_limit() -> tuple[int, int]:
    limit = int(os.environ.get("RATE_LIMIT_WEBHOOK_IP_LIMIT", str(DEFAULT_WEBHOOK_IP_LIMIT)))
    window_s = int(
        os.environ.get("RATE_LIMIT_WEBHOOK_IP_WINDOW_S", str(DEFAULT_WEBHOOK_IP_WINDOW_S))
    )
    return limit, window_s


def _client_ip(request: Request) -> str:

    A forged leading entry only picks its own bucket; the limit sits before the real JWT check.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client is not None else "unknown"


def _check_webhook_ip_rate_limit(request: Request, bucket: str) -> None:
    ip = _client_ip(request)
    limit, window_s = _webhook_ip_rate_limit()
    if not rate_limits_store.check_and_increment(
        f"webhook_ip:{bucket}:{ip}", limit=limit, window_s=window_s
    ):
        raise HTTPException(status_code=429, detail="too many requests")

TopicHandler = Callable[[Ingest, str, bytes], None]

_TOPIC_SUFFIX_HANDLERS: dict[str, TopicHandler] = {
    "up": lambda ingest, topic, payload: ingest.handle_up(topic, payload),
    "status": lambda ingest, topic, payload: ingest.handle_status(topic, payload),
    "loc": lambda ingest, topic, payload: ingest.handle_loc(topic, payload),
}


def _topic_suffix(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "pager":
        return None
    return parts[2]


@router.post("/webhooks/mqtt")
async def mqtt_webhook(request: Request) -> Response:
    broker: BrokerClient = request.app.state.broker
    if not broker.verify_webhook(request):
        raise HTTPException(status_code=401, detail="invalid or missing webhook key")

    body = await request.body()
    parsed = broker.parse_webhook(body)
    if parsed is None:
        return Response(status_code=200)

    topic, payload, _qos = parsed
    if handler is None:
        logger.warning("webhook for unrecognised topic %s dropped", topic)
        return Response(status_code=200)

    ingest: Ingest = request.app.state.ingest
    try:
        # off the event loop: handlers block on Firestore and up to REPUBLISH_CAP broker publishes
    except GoogleAPICallError:
        # a storage failure is not a 200: the message would be lost; a 500 makes the broker retry
        logger.exception("store error handling webhook for topic %s", topic)
        raise
    except Exception:
        # an ingest bug must not become a non-2xx that retry-storms the broker (§3.4)
        logger.exception("unexpected error handling webhook for topic %s", topic)
    return Response(status_code=200)


def _reject_hint(reason: str) -> str:
    return "unknown recipient" if reason == "unknown_alias" else "not allowed to message that recipient"


def _chat_json(body: dict[str, str]) -> Response:
    import json

    return Response(
        content=json.dumps(body), status_code=200, media_type="application/json"
    )


@router.post("/webhooks/gchat")
async def gchat_webhook(request: Request) -> Response:
    _check_webhook_ip_rate_limit(request, "gchat")
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    # `partition`: a bare `"Bearer "` header must be a 401, not an IndexError 500
    token = auth_header.partition(" ")[2].strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")
    try:
        gchat_backend.verify_chat_bearer_token(token, audience=gchat_backend.gchat_audience())
    except Exception as exc:  # noqa: BLE001 -- verify_chat_bearer_token's own contract: any failure -> 401
        logger.warning("gchat webhook rejected invalid JWT: %r", exc)
        raise HTTPException(status_code=401, detail="invalid Chat token") from None

    try:
        payload = await request.json()
    except ValueError:
        return _chat_json({})
    if not isinstance(payload, dict):
        return _chat_json({})

    message = payload.get("message") or {}
    space_obj = message.get("space") or payload.get("space") or {}
    space = space_obj.get("name") if isinstance(space_obj, dict) else None
    # Chat space `type`: "DM" for 1:1, "ROOM"/"SPACE" for groups
    space_type = space_obj.get("type") if isinstance(space_obj, dict) else None
    sender_obj = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    sender_name = sender_obj.get("name")
    text = (message.get("text") or "").strip()
    if not space:
        return _chat_json({})
    space_id = space.rsplit("/", 1)[-1]

    if text.lower().startswith("/link"):
        parts = text.split(None, 1)
        code = parts[1].strip() if len(parts) == 2 else ""
        popped = backends_store.pop_gchat_link_code(code) if code else None
        if popped is None:
            return _chat_json({"text": "That code is invalid or already used."})
        uid, bid, expires_at = popped
        if time.time() > expires_at:
            return _chat_json(
                {"text": "That code expired -- get a new one from Settings > Backends."}
            )
        if space_type != "DM":
            # reject non-DM spaces: any member of a group could send as the linked user
            logger.warning(
                "SECURITY gchat link rejected: space %s is not a DM (type=%r)", space_id, space_type
            )
            return _chat_json(
                {"text": "Pager can only be linked from a direct message, not a group space."}
            )
        row = backends_store.get_backend(uid, bid)
        config = dict(row.config) if row is not None else {}
        config.pop("linkCode", None)
        config.pop("linkCodeExpiresAt", None)
        config["space"] = space
        backends_store.update_backend(uid, bid, config=config, verified=True, enabled=True)
        # pin the linking message's sender; later messages are checked against it
        backends_store.set_gchat_space(space_id, uid, bid, sender_name)
        return _chat_json({"text": "Linked! You can now send messages from here."})

    match = backends_store.get_by_gchat_space(space_id)
    if match is None:
        return _chat_json(
            {"text": "This space isn't linked yet -- send /link <code> from Settings > Backends."}
        )
    uid, bid, linked_sender = match
    if linked_sender is not None and sender_name != linked_sender:
        # another member of the space: never attribute it to the linked user
        # (`linked_sender is None` = linked before pinning; allowed)
        logger.warning(
            "SECURITY gchat message from sender %r in space %s does not match linked sender %r "
            "(bid=%s) -- ignored",
            sender_name,
            space_id,
            linked_sender,
            bid,
        )
        return _chat_json({})

    resolved = resolve_reply(uid, text)
    if resolved is None:
        return _chat_json({"text": USAGE_HINT})

    routing: Routing = request.app.state.routing
    result = routing.send(
        sender_uid=uid,
        recipient_alias=resolved.recipient_alias,
        kind="text",
        body=resolved.body,
        origin_backend_kind="gchat",
        origin_backend_id=bid,
    )
    if result.rejected:
        return _chat_json({"text": _reject_hint(result.rejected[0].reason)})
    return _chat_json({})
