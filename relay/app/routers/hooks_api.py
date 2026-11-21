"""`/hooks/v1/{hookId}`: the hook callback API, authenticated by the hook's bearer secret.

Limited to 120 calls a minute per hook and to the families the hook is registered for."""

from __future__ import annotations

import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import hooks as hooks_module
from app.store import conversations as conversations_store
from app.store import hooks as hooks_store
from app.store import messages as messages_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.store.hooks import Hook

logger = logging.getLogger("relay.hooks")

router = APIRouter(prefix="/hooks/v1/{hook_id}")

RATE_LIMIT = 120
RATE_WINDOW_S = 60
HISTORY_LIMIT_MAX = 200


def current_hook(
    hook_id: str, authorization: Annotated[str | None, Header()] = None
) -> Hook:
    hook = hooks_store.get(hook_id)
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    # The same answer for "no such hook" and "wrong secret".
    if hook is None or not hmac.compare_digest(token.encode(), hook.secret.encode()):
        raise HTTPException(status_code=401, detail="bad hook credentials")
    if not hook.enabled:
        raise HTTPException(status_code=403, detail="hook disabled")
    if not rate_limits_store.check_and_increment(
        f"hook:{hook.id}", limit=RATE_LIMIT, window_s=RATE_WINDOW_S
    ):
        raise HTTPException(status_code=429, detail="hook rate limit exceeded")
    return hook


HookAuth = Annotated[Hook, Depends(current_hook)]


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    body: str = ""


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str | None = None
    messageId: str | None = None
    alert: AlertIn | None = None
    labels: list[str] | None = None
    reason: str | None = Field(default=None)


class ActionResult(BaseModel):
    updated: int
    alerts: int


@router.get("/ping")
def ping(hook: HookAuth) -> dict[str, object]:
    return {"ok": True, "hook": hook.id, "name": hook.name, "mode": hook.mode}


@router.post("/actions")
def actions(req: ActionRequest, hook: HookAuth) -> ActionResult:
    """Apply an `alert` / `labels` / `reason` after the fact; delivered pages cannot be unsent."""
    if bool(req.ref) == bool(req.messageId):
        raise HTTPException(
            status_code=422, detail="give exactly one of ref or messageId"
        )
    if req.messageId:
        found = messages_store.get_message(req.messageId)
        rows = [found.model_dump()] if found is not None else []
    else:
        rows = hooks_store.messages_by_ref(req.ref or "")
    rows = [r for r in rows if hook.covers(set(r.get("familyIds") or []))]
    if not rows:
        raise HTTPException(status_code=404, detail="no such message for this hook")

    labels = hooks_module.clean_labels(req.labels)
    reason = req.reason[: hooks_module.REASON_MAX] if req.reason else None
    now = int(time.time())
    if labels or reason:
        for r in rows:
            hooks_store.annotate_message(
                r["id"], hook.id, labels=labels, reason=reason, at=now
            )

    raised = 0
    if req.alert is not None and (req.alert.title or req.alert.body):
        first = rows[0]
        family_ids = {f for r in rows for f in (r.get("familyIds") or [])}
        raised = hooks_module.raise_flag(
            hook,
            family_ids=family_ids,
            conv_key=first.get("convKey"),
            subject=users_store.get_user(first.get("senderUid") or ""),
            title=req.alert.title,
            body=req.alert.body,
        )
    return ActionResult(updated=len(rows) if (labels or reason) else 0, alerts=raised)


@router.get("/conversations/{key}/messages")
def conversation_messages(
    key: str,
    hook: HookAuth,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=HISTORY_LIMIT_MAX)] = 50,
) -> dict[str, list[dict]]:
    """History in the payload `context` shape, oldest first; `before` is an earlier `id`."""
    conv = conversations_store.get_conversation(key)
    if conv is None or not hook.covers(set(conv.familyIds)):
        raise HTTPException(
            status_code=404, detail="no such conversation for this hook"
        )
    return {
        "messages": hooks_module.conversation_history(
            key, limit=limit, group=conv.kind == "group", before_id=before
        )
    }
